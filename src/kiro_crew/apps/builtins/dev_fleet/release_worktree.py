"""Create one detached worktree pinned to the newest stable release.

A single owner-initiated action that materializes ``release-channel-stable``
as a sibling worktree detached at the newest published ``vX.Y.Z`` tag, so an
operator can pod it or Make Live onto it. It runs no background tag mirror,
pins no fleet row and changes no prune rule: it is the create and nothing else.

WHERE IT RUNS, AND WHY. The checkout is driven from the GATEWAY process
(``gateway_routes.handle_create_release_worktree``), never the sandboxed Dev
Fleet backend. ``git worktree add <tag>`` reads the repository's own
``.git/config`` and follows ``include.path`` during checkout, so a hostile
config could name a ``filter.<name>.smudge`` driver or point ``include.path``
at a credential file. The gateway can give that checkout a FRESH strict-tier
sandbox (``~/.aws``, ``~/.ssh`` bind-masked away); the backend cannot -- a
``mode="strict"`` spawn from inside the already-sandboxed backend is a nested
passthrough that re-applies only the env scrub, not the file hides. So the
same checkout the dashboard's ``handlers/worktree.py`` performs for the
follow-up card -- strict tier, ``core.hooksPath``/``core.fsmonitor`` off,
config hooks disabled by name, and a refusal when a checkout filter is
declared -- is reused here verbatim rather than re-implemented.

RESOLUTION IS FROM WHAT THE REMOTE ADVERTISES, not a local ref store. The
newest stable tag and its commit come from ``git ls-remote`` against the
configured upstream, so a tag someone wrote into the local ``refs/tags/`` can
neither rename the action's target nor supply the oid it checks out. The
``advertised_release_commit`` confirm is the integrity gate: the oid checked out
is the one the remote publishes for that tag, or the action refuses.

THE FETCH is a single ``git fetch`` of that one tag's objects, writing no ref
and no ``FETCH_HEAD``, when the action runs; the action adds no background tag fetch.

THE TRUST BOUNDARY, in one statement. No git child this action runs is ever LESS
confined than an agent's own commands. The local steps (the worktree-list,
``_checkout_filter`` and promisor probes, ``rev-parse``, ``worktree add``, the HEAD
readback, cleanup) run STRICT via ``_run_git``. The route's main-checkout discovery
(``gateway_routes._ensure_repo``), which runs before this module, is outside that
statement. The two network steps (``ls-remote``, ``fetch``) run at the STRICTER of
``standard`` and this install's effective ``agent.sandbox`` tier (``_network_tier``):
on the default ``agent.sandbox = standard`` that is ``standard``, and on a ``cc`` or
``strict`` install they clamp UP to that tier rather than running at a hardcoded
``standard`` above it. ``runtime._run_cmd`` then applies any governed
``sandbox.min_level`` floor on top, exactly as it does for the agent's own spawns, so
the result is never below the agent's effective tier on any host. So a
worktree-controlled ``.git`` -- a planted ``filter.<name>`` driver, a redirected
``remote.<name>.url`` / ``url.<base>.insteadOf``, a ``refs/replace`` graft, a swapped
ref -- gains the agent that wrote it NOTHING it could not already do by running code
at its own tier: any credential a redirected remote could reach at ``standard`` is one
the agent's own ``standard`` commands already reach, and at ``cc``/``strict`` the
network steps run at the agent's tier with git credentials withheld. The strict local
steps additionally mask ``~/.aws``/``~/.ssh`` wherever an OS sandbox backend runs them,
and the fetch's env neutralizers pin transports to https/ssh and reset
``core.sshCommand``/``credential.helper``/gpg; the filter and promisor probes and those
neutralizers are therefore early, friendly refusals, not the boundary. The statement above does not
cover an agent session whose isolation Kiro Crew leaves to the Kiro CLI's own sandbox
(on macOS with that sandbox switched on, or the official Kiro CLI on Windows), which
these tiers do not confine.

THE COST OF THAT CLAMP, stated plainly. At ``cc``/``strict`` ``_run_cmd`` withholds the
credential-helper env (it adds it only at ``standard``), so a PRIVATE remote cannot
authenticate: the action refuses with a worded reason that names the tier. A PUBLIC
remote is unaffected, and the default ``standard`` install is unchanged.

Provenance is thus the whole ``.git`` -- config, refs AND objects -- exactly what
Dev Fleet's sync pull, rebase and Make Live already trust. (git's content-address
check does defend the OBJECTS: a loose object whose bytes do not hash to its oid is
refused on read, and the local object steps pass ``--no-replace-objects``, so a
``refs/replace`` graft cannot swap the checked-out tree; but a redirected remote is
honored, so the boundary is the whole ``.git``, not the object store alone.) The per-request
``ls-remote`` + ``advertised_release_commit`` check guards against a STALE or
PLANTED LOCAL TAG -- a tag someone wrote into ``refs/tags/`` -- not against a writer
of ``.git``; anchoring the fetch at a fixed URL would not change that, so this
module does not.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from kiro_crew import sandbox_tiers
from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.version import parse_version

# The checkout-safety helpers are the dashboard follow-up card's, reused as-is
# rather than copied: `_checkout_filter` refuses a repo that declares a content
# filter, `_run_git` runs git at the strict tier with hooks/fsmonitor off and
# config hooks disabled by name, and `SandboxUnavailable` is its fail-closed
# signal when no OS sandbox backend exists and unsandboxed execution is not
# permitted. Importing them keeps this action on exactly the security-reviewed
# path, and a change to that path reaches here too.
from kiro_crew.dashboard.handlers.worktree import (
    SandboxUnavailable,
    _checkout_filter,
    _run_git,
)
from kiro_crew.loop_lock import LoopBoundLock

#: The characters one part of a release tag may hold. ``str.isdigit`` would also accept
#: the digits of other scripts, which ``int()`` reads, so ``v\u0669.0.0`` (an
#: Arabic-Indic nine) would outrank every real release.
_ASCII_DIGITS = frozenset("0123456789")

#: The most digits one part of a release tag may carry, which keeps
#: ``parse_version``'s ``int()`` far below the length it refuses to convert.
_MAX_TAG_PART_DIGITS = 9

#: Basename of the worktree this action creates -- the fleet row label and the
#: pod identity, spelled in full so it reads as what it is beside a
#: ``kirocrew-wt-<slug>`` feature worktree.
WORKTREE_NAME = "release-channel-stable"

#: The characters of an object id as git prints it, lower-case hex.
_HEX_DIGITS = frozenset("0123456789abcdef")

#: The lengths of an object id as git prints it: sha1 and sha256, nothing between.
_OID_LENGTHS = (40, 64)

#: git's spellings of a false boolean, lower-cased, an empty value among them. git reads
#: every other value as true (any non-zero integer included), so anything else counts.
_GIT_FALSE = frozenset({"false", "no", "off", "0", ""})

#: The most bytes a single network step's combined output may buffer in the gateway
#: process before the child is killed and the step refuses. An ``ls-remote`` ref list and
#: a fetch's sideband are both agent-influenced -- the remote is the agent's to pick via
#: ``remote.<n>.url`` / ``url.<base>.insteadOf`` -- so a hostile advertisement is bounded
#: here. 4 MiB is far above any honest one-tag exchange and far below gateway pressure.
_MAX_NETWORK_OUTPUT_BYTES = 4 * 1024 * 1024

#: git's global option that turns ``refs/replace`` lookup off, for this process and
#: (through ``GIT_NO_REPLACE_OBJECTS``) every git it spawns. Every local step that reads
#: or checks out the release's objects passes it, so a replacement ref cannot swap the
#: advertised commit's tree for another while ``HEAD`` still reads the advertised oid.
_NO_REPLACE = "--no-replace-objects"

#: How ``runtime._run_cmd`` words the refusal it returns, as ``(-1, "", ...)``, when no OS
#: sandbox backend can run a step and unsandboxed execution is not permitted. A step that
#: meets it answers ``sandbox_unavailable`` -- the same code the strict local steps answer
#: when ``_run_git`` raises :class:`SandboxUnavailable` -- so a missing sandbox reads the
#: same on every step.
_SANDBOX_REFUSAL_PREFIX = "sandbox unavailable:"

#: Serializes concurrent create requests within the gateway event loop. The action
#: is owner-only and runs in one process, so one lock is the whole of the
#: concurrency story: a second request while one create runs waits, then finds the
#: worktree already present and refuses cleanly.
_CREATE_LOCK = LoopBoundLock()


def _error_detail(err: str) -> str:
    """A git stderr reduced to one redacted, length-bounded line for a result message.

    ``runtime._redact`` scrubs credential-shaped tokens; the last non-empty line is
    git's actual ``fatal:``/``error:`` and the rest is noise, and 200 chars is plenty
    for a UI notice.
    """
    text = runtime._redact((err or "").strip())
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:200]


def _network_tier() -> str:
    """The sandbox tier the two network steps (``ls-remote``, ``fetch``) run at.

    The STRICTER of ``standard`` and this install's effective ``agent.sandbox`` tier,
    so a network git child is never LESS confined than an agent's own commands, within
    the scope the module docstring states. The result is always a canonical tier name:
    the shipped default ``agent.sandbox = auto`` is mapped through the sandbox layer's
    ``_SANDBOX_MODE_ALIASES`` to ``standard``, because ``_run_cmd`` and
    :func:`_private_remote_hint` compare against the literal ``standard``. On that
    default this is ``standard`` and nothing changes; on a ``cc``/``strict`` install it returns
    that tier, clamping the network steps UP from the hardcoded ``standard`` they would
    otherwise run at. ``runtime._run_cmd`` then applies any governed
    ``sandbox.min_level`` floor on top, exactly as it does for the agent's own spawns,
    so the result is never below the agent's effective tier.

    The cost is disclosed by :func:`_private_remote_hint`: at ``cc``/``strict``
    ``_run_cmd`` withholds the credential-helper env, so a PRIVATE remote cannot be
    fetched and the action refuses; a public remote is unaffected.

    ``configured_sandbox_mode`` reads the config file and the governance floor
    (blocking I/O), so a caller resolves this ONCE per action OFF the event loop
    (``await asyncio.to_thread(_network_tier)``) and threads the result into the
    network steps, rather than calling it on the loop.

    The clamp to ``standard`` is the sandbox layer's own
    ``_clamp_sandbox_mode_to_floor``, which reads the strictness order from
    governance (``_ORDINAL_SCALES["sandbox"]``), so a tier added there is honoured
    here without a private copy of the order.
    """
    agent = sandbox_tiers.effective_sandbox_mode(sandbox_tiers.configured_sandbox_mode())
    mode = sandbox_tiers._clamp_sandbox_mode_to_floor(agent, "standard")
    return sandbox_tiers._SANDBOX_MODE_ALIASES.get(mode, mode)


def _private_remote_hint(mode: str) -> str:
    """A worded tail for a failed network step that ran ABOVE ``standard``.

    At ``cc``/``strict`` the credential-helper env is withheld (``_run_cmd`` adds it
    only at ``standard``), so a private remote cannot authenticate and the step fails.
    The hint names the tier and that consequence so the refusal is actionable. Empty at
    ``standard``, where credentials are present and a failure means something else.
    """
    if mode == "standard":
        return ""
    return (
        f" (the network step ran at the '{mode}' tier to match this install's "
        "agent.sandbox, which withholds git credentials; a private remote cannot be "
        "reached at this tier, though a public one is unaffected)"
    )


def _sandbox_reason(err: str) -> str | None:
    """The reason inside ``_run_cmd``'s no-sandbox refusal, or ``None`` for any other error."""
    if err.startswith(_SANDBOX_REFUSAL_PREFIX):
        return err[len(_SANDBOX_REFUSAL_PREFIX) :].strip()
    return None


def _sandbox_refusal(reason: str) -> dict:
    """The result for a step refused because no OS sandbox backend exists and unsandboxed
    execution is not permitted. The reason is redacted: it can be a line read from the
    git child's stderr."""
    return {"ok": False, "code": "sandbox_unavailable", "error": runtime._redact(reason)}


async def _run_network_git(cmd: list[str], *, mode: str, timeout: int) -> tuple[int, str, str]:
    """Run one network git step (``ls-remote`` / ``fetch``) for this action.

    This action's own runner for the two steps that reach an agent-selected REMOTE from
    the GATEWAY process. It is :func:`runtime._run_cmd` with this action's two opt-ins
    engaged and nothing else changed, so the whole reviewed spawn path -- trusted-bin
    pin, ``_GIT_ENV_NEUTRALIZERS``, ``sandboxed_spawn_argv``, the RLIMIT ceiling, and the
    kill/reap on timeout and cancellation -- is reused rather than reimplemented here:

    * ``sandboxed_hook_scan=True`` runs the hook-name scan INSIDE the sandbox, so the
      gateway never runs an unsandboxed git against a repo-controlled config merely to
      list its hook names (matching ``dashboard/handlers/worktree.py``'s ``_run_git``);
    * ``max_output_bytes=_MAX_NETWORK_OUTPUT_BYTES`` drains stdout/stderr incrementally,
      kills the child past the cap, and refuses with a worded reason, so a hostile
      advertisement cannot fill the gateway process.

    ``mode`` is the network-step tier the caller resolved off the loop
    (:func:`_network_tier`); it drives both the sandboxed scan and the real call. Every
    other security property is ``_run_cmd``'s, by construction.

    Raises :class:`SandboxUnavailable` when ``_run_cmd`` refuses for want of an OS
    sandbox backend, so a network step fails exactly as a strict local step does.
    """
    rc, out, err = await runtime._run_cmd(
        cmd,
        timeout=timeout,
        mode=mode,
        sandboxed_hook_scan=True,
        max_output_bytes=_MAX_NETWORK_OUTPUT_BYTES,
    )
    reason = _sandbox_reason(err) if rc == -1 else None
    if reason is not None:
        raise SandboxUnavailable(reason)
    return rc, out, err


def is_release_tag(tag: str) -> bool:
    """Whether *tag* names a stable release this action will check out.

    A release is ``v`` and exactly three dot-separated parts, read whole, so ``v1.2.3``
    with anything after it (a newline included) is no release. Each part is one to
    :data:`_MAX_TAG_PART_DIGITS` ASCII digits, either ``0`` or with no leading zero. A
    prerelease suffix (``-rc.N``, ``-insider.N``) is deliberately excluded: ordering
    those against each other needs a precedence this repository does not state, and the
    action materializes the one channel whose tip a bare tag already names.
    """
    if not tag.startswith("v"):
        return False
    parts = tag[1:].split(".")
    if len(parts) != 3:
        return False
    for part in parts:
        if not part or len(part) > _MAX_TAG_PART_DIGITS or not set(part) <= _ASCII_DIGITS:
            return False
        if part != "0" and part.startswith("0"):
            return False
    return True


def worktree_path(repo: str) -> str:
    """Where the release worktree lives: a sibling of the primary checkout."""
    return str(Path(repo).parent / WORKTREE_NAME)


def _promisor_in_force(config_list: str) -> bool:
    """Whether ``git config --list -z`` output names a promisor remote in force.

    git records a promisor remote two ways: ``extensions.partialClone`` (a remote name;
    set at all means partial) and ``remote.<name>.promisor`` (a boolean -- a hydrated
    clone can keep it as ``false``). Each entry of the listing is ``key`` and ``value``
    joined by a newline and ended by a NUL, and a key with no value is the bare key, so a
    value that holds a newline cannot pass for a key. Keys are compared exactly, after
    lower-casing (git's section and variable names are case-insensitive).

    Read by value, and closed: a ``remote.<name>.promisor`` counts unless it holds one of
    git's false spellings, so a ``false`` left behind by a hydrated clone is no partial
    clone while ``2`` is one. The bare key is true and an empty value is false. A rare
    false spelling outside the set (``00``) reads as in force and is refused, the safe
    side of the mistake.
    """
    for entry in config_list.split("\0"):
        key, sep, value = entry.partition("\n")
        key = key.lower()
        if key == "extensions.partialclone":
            return True
        if key.startswith("remote.") and key.endswith(".promisor"):
            if not sep or value.strip().lower() not in _GIT_FALSE:
                return True
    return False


async def _advertised_stable_tags(repo: str, mode: str) -> tuple[list[str] | None, str | None]:
    """Stable tag NAMES the configured remote advertises, or ``(None, error)``.

    One ``ls-remote`` against the upstream, filtered server-side to ``refs/tags/v*``.
    Only the names are read here; the authoritative commit for the chosen tag comes
    from :func:`advertised_release_commit`. ``refs/tags/v*`` also lists names that are
    no release (``vendor-*``, a long feature tag), and those are dropped without being
    counted or measured, so none of them can refuse the action. The listing itself is
    bounded by :data:`_MAX_NETWORK_OUTPUT_BYTES`, and every name kept has the shape
    :func:`is_release_tag` admits, nine digits per part at most.

    *mode* is the network-step sandbox tier the caller resolved off the loop
    (:func:`_network_tier`). Raises :class:`SandboxUnavailable` when no OS sandbox
    backend can run the query and unsandboxed execution is not permitted.
    """
    remote = await repository._upstream_remote()
    rc, out, err = await _run_network_git(
        ["git", "-C", repo, "ls-remote", "--tags", "--refs", remote, "refs/tags/v*"],
        mode=mode,
        timeout=60,
    )
    if rc != 0:
        detail = _error_detail(err)
        shown = runtime._redact(remote)
        return None, (detail or f"git ls-remote {shown} failed") + _private_remote_hint(mode)
    rows = [line.strip() for line in (out or "").splitlines() if line.strip()]
    tags: list[str] = []
    for row in rows:
        parts = row.split()
        if len(parts) != 2:
            continue
        _oid, ref = parts
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/") :]
        if is_release_tag(tag):
            tags.append(tag)
    return tags, None


async def advertised_release_commit(
    repo: str, tag: str, mode: str | None = None
) -> tuple[str | None, str | None]:
    """The commit oid the CONFIGURED remote advertises for *tag*, or an error.

    The integrity gate: it asks the
    upstream what it publishes for exactly ``refs/tags/<tag>`` and its ``^{}``
    peel, so "published" is a verified fact about that remote rather than an
    inference from a local ref. The peel wins, because an annotated tag's
    ``refs/tags/vX.Y.Z`` line carries the tag OBJECT's oid and only the following
    ``^{}`` line carries the commit a checkout must land on.

    ``(None, None)`` means the remote does not carry the tag (``ls-remote`` exits 0
    with no output); a non-zero exit is the query itself failing and returns an
    error, failing closed, and a host with no OS sandbox backend that does not permit
    unsandboxed execution raises :class:`SandboxUnavailable`. The remote URL is read from
    this repository's git config at query time, so the guarantee binds the checkout to
    what the configured upstream advertises -- not to a writer who can also rewrite
    ``remote.<n>.url``, who already redirects every fetch this app performs.
    """
    if not is_release_tag(tag):
        raise ValueError(f"not a release tag: {tag!r}")
    # *mode* is resolved off the loop by the caller; a direct caller that omits it
    # resolves it the same way, off the loop, rather than blocking on the config read.
    if mode is None:
        mode = await asyncio.to_thread(_network_tier)
    remote = await repository._upstream_remote()
    direct_ref = f"refs/tags/{tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    rc, out, err = await _run_network_git(
        ["git", "-C", repo, "ls-remote", "--tags", remote, direct_ref, peeled_ref],
        mode=mode,
        timeout=60,
    )
    if rc != 0:
        detail = _error_detail(err)
        shown = runtime._redact(remote)
        return None, (detail or f"git ls-remote {shown} failed") + _private_remote_hint(mode)
    direct: str | None = None
    peeled: str | None = None
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        oid, ref = parts
        if len(oid) not in _OID_LENGTHS or not set(oid) <= _HEX_DIGITS:
            continue
        if ref == direct_ref:
            direct = oid
        elif ref == peeled_ref:
            peeled = oid
    return peeled or direct, None


async def _resolve_newest_stable(repo: str, mode: str | None = None) -> dict:
    """Pick the newest stable tag the remote advertises and confirm its commit.

    ``{"ok": True, tag, version, oid}`` on success. A remote that advertises no
    stable tag is ``{"ok": False, "unpublished": True, ...}`` -- a real, benign
    answer kept distinct from a git failure (``{"ok": False, "error": ...}``) so a
    caller can word the two differently.

    The newest NAME comes from the advertised list; its oid is then CONFIRMED with
    :func:`advertised_release_commit`. A tag whose confirm disagrees with the list,
    or that the confirm does not carry, is refused rather than checked out -- the
    window between the two reads is closed on the side that matters, the oid a
    checkout would land on. Raises :class:`SandboxUnavailable` from either query.
    """
    if mode is None:
        mode = await asyncio.to_thread(_network_tier)
    tags, err = await _advertised_stable_tags(repo, mode)
    if tags is None:
        return {"ok": False, "error": err or "cannot list the remote's release tags"}
    if not tags:
        return {
            "ok": False,
            "unpublished": True,
            "error": "No stable release found on the remote yet. Nothing was created.",
        }
    # `parse_version` is the repo's one version parser; every tag here passed
    # `is_release_tag`, so it is a bare `vX.Y.Z` and the parse cannot raise.
    newest = sorted(tags, key=lambda t: parse_version(t[1:]), reverse=True)[0]
    oid, adv_err = await advertised_release_commit(repo, newest, mode)
    if adv_err:
        return {"ok": False, "error": f"cannot confirm the release with the remote: {adv_err}"}
    if not oid:
        # Advertised a moment ago in the list, absent on the confirm: the tag was
        # retracted between the two reads. Refuse rather than check out a tag the
        # remote does not publish.
        return {
            "ok": False,
            "error": f"the remote does not advertise {runtime._redact(newest)}",
        }
    return {"ok": True, "tag": newest, "version": newest[1:], "oid": oid}


def _dest_refusal(repo: str, dest: str) -> dict | None:
    """Why *dest* is not a safe place to create the worktree, or ``None``.

    The destination is DERIVED here (``worktree_path``) from the primary checkout's
    own path -- never supplied by a caller -- so it is the ``release-channel-stable``
    sibling by construction; that derivation, not a parent-path comparison, is what
    keeps the checkout inside the checkout's own directory. This is only the early,
    friendly check that nothing already sits at the path;
    :func:`create_release_worktree` then claims the path atomically with ``os.mkdir``
    before the checkout, and THAT is what closes the window against a concurrent
    writer. Synchronous filesystem checks; the caller runs them off the loop.
    """
    if os.path.basename(dest) != WORKTREE_NAME:
        return {"ok": False, "code": "bad_dest", "error": "unexpected worktree name"}
    if os.path.lexists(dest):
        return {
            "ok": False,
            "code": "path_exists",
            "error": f"{WORKTREE_NAME} already exists -- remove it first, then create it again",
        }
    return None


async def _release_claim(dest: str) -> None:
    """Give back the directory this request claimed, when git left nothing of its own.

    For a ``worktree add`` that exited on its own or never started. An add that exits with
    an error removes what it created itself, so what can remain at ``dest`` is an empty
    directory, normally the one :func:`_claim_and_check_out` made -- or, when the add
    refused because another writer took the path after the claim, that writer's tree.
    ``os.rmdir`` removes only an empty directory, so the claim is released and no
    non-empty tree is touched. An add killed by a signal (an OOM kill, a CPU limit) exits
    non-zero too and leaves its partial tree, which then stays for the operator.
    """
    try:
        await asyncio.to_thread(os.rmdir, dest)
    except OSError:
        pass  # already gone, or not empty: not this request's to remove


async def _remove_own_worktree(repo: str, dest: str) -> str:
    """Remove the worktree this request's own ``worktree add`` registered.

    Called only after the add exited 0 and the readback refused the tree: git has
    finished, and the record at ``dest`` is the one it made. The removal is forced twice
    so a lock on the record cannot stop it. git does the removal and nothing here
    deletes the path by name, so a tree another writer puts there once this one is gone
    is never touched. Returns ``""`` when git removed it; otherwise the tree is left in
    place and the returned note, for the refusal, names the command that clears it. A
    cleanup failure never replaces the caller's error.
    """
    try:
        proc = await asyncio.to_thread(
            _run_git, ["worktree", "remove", "--force", "--force", dest], repo
        )
    except Exception:  # noqa: BLE001 - the caller's error is the news
        runtime.logger.warning(
            "release-worktree: cleanup after a failed create raised", exc_info=True
        )
    else:
        if proc.returncode == 0:
            return ""
        runtime.logger.warning(
            "release-worktree: cleanup after a failed create failed: %s",
            _error_detail(proc.stderr),
        )
    return _left_note(dest)


def _left_note(dest: str) -> str:
    """The refusal's note for a tree this action could not remove, naming the command."""
    shown = runtime._redact(dest)
    return f"; the partial worktree was left at {shown} -- clear it with `git worktree remove -f -f {shown}`"


async def _claim_and_check_out(repo: str, dest: str, oid: str) -> dict | None:
    """Claim ``dest``, check ``oid`` out into it detached, and verify it landed.

    One transaction: the caller awaits it through ``runtime._run_uninterruptible``, so a
    cancellation cannot stop it between the claim and the readback or the cleanup.
    Returns ``None`` when the tree landed, or the refusal dict. On a failure after the
    claim, the unwind removes at most an empty directory or this request's own tree --
    except after a timeout, when the checkout may still be finishing and nothing is
    removed.
    """
    # Claim the destination atomically before the checkout. `git worktree add`
    # accepts an empty directory, and `os.mkdir` fails closed if anything already
    # holds the reserved path -- a concurrent create, a manual checkout -- in the
    # window since `_dest_refusal`'s check. The unwinds below then remove at most an
    # empty directory or this request's own tree: the claimed directory while it is
    # empty, or the tree and record this request's own add made.
    try:
        await asyncio.to_thread(os.mkdir, dest)
    except FileExistsError:
        return {
            "ok": False,
            "code": "path_exists",
            "error": f"{WORKTREE_NAME} already exists -- remove it first, then create it again",
        }
    except OSError as exc:
        return {"ok": False, "code": "bad_dest", "error": runtime._redact(str(exc))}

    # The checkout. Strict tier, hooks/fsmonitor off, config hooks disabled by
    # name -- `_run_git`'s contract. `--detach` so the tree is a fixed point the
    # fleet renders as an ordinary row; a branch would invite a `git pull` that
    # drifts it off the release. Every failure refuses. A timeout removes nothing:
    # the checkout may still be finishing, and a removal now would race it, so the
    # refusal names the command that clears the tree once it stops. Any other failure
    # only releases the claim, with an rmdir that removes at most an empty directory: an
    # add that exits with an error has removed what it created, and one refused for
    # another writer's tree must leave that tree.
    try:
        proc = await asyncio.to_thread(
            _run_git, [_NO_REPLACE, "worktree", "add", "--detach", dest, oid], repo
        )
    except SandboxUnavailable as exc:
        await _release_claim(dest)
        return _sandbox_refusal(str(exc))
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "code": "git_failed",
            "error": (
                f"git worktree add did not complete: {runtime._redact(str(exc))}; it may "
                f"still be finishing at {runtime._redact(dest)} -- once it stops, clear what "
                f"it leaves with `git worktree remove -f -f {runtime._redact(dest)}`"
            ),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        await _release_claim(dest)
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"git worktree add did not complete: {runtime._redact(str(exc))}",
        }
    if proc.returncode != 0:
        await _release_claim(dest)
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"git worktree add failed: {_error_detail(proc.stderr)}",
        }

    # Confirm the tree landed detached at the oid we resolved: a plain file read
    # of the new worktree's HEAD, so a `worktree add` that reported success but
    # produced something else is caught before the row is published.
    try:
        landed = await asyncio.to_thread(_run_git, [_NO_REPLACE, "rev-parse", "HEAD"], dest)
        symref = await asyncio.to_thread(_run_git, ["symbolic-ref", "--quiet", "HEAD"], dest)
    except SandboxUnavailable as exc:
        left = await _remove_own_worktree(repo, dest)
        return _sandbox_refusal(str(exc) + left)
    except (OSError, subprocess.SubprocessError) as exc:
        left = await _remove_own_worktree(repo, dest)
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"the created worktree could not be verified: {runtime._redact(str(exc))}{left}",
        }
    if landed.returncode != 0 or landed.stdout.strip() != oid or symref.returncode == 0:
        left = await _remove_own_worktree(repo, dest)
        return {
            "ok": False,
            "code": "git_failed",
            "error": f"the created worktree is not detached at the resolved release{left}",
        }
    return None


async def create_release_worktree() -> dict:
    """Materialize ``release-channel-stable`` detached at the newest stable release.

    Resolves the tag and its advertised commit from the remote, fetches only that
    one tag's objects, refuses a repo that declares a checkout filter, and runs a
    strict-tier ``git worktree add --detach`` from the gateway. Returns a result
    dict: ``{"ok": True, path, version, ref, oid}`` or ``{"ok": False, code,
    error}``. Single-flight under :data:`_CREATE_LOCK`; the claim, checkout,
    readback and cleanup settle as one transaction even when the request is
    cancelled (:func:`_claim_and_check_out`).
    """
    repo = repository._repo()
    loop = asyncio.get_running_loop()
    async with _CREATE_LOCK:
        # Resolve the network-step tier ONCE, off the loop, and thread it into every
        # network step: `_network_tier` reads the config file and the governance
        # floor, which must not block the gateway's event loop.
        mode = await asyncio.to_thread(_network_tier)
        try:
            resolved = await _resolve_newest_stable(repo, mode)
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        if not resolved.get("ok"):
            code = "unpublished" if resolved.get("unpublished") else "unresolved"
            return {"ok": False, "code": code, "error": resolved["error"]}
        tag = str(resolved["tag"])
        oid = str(resolved["oid"])
        # The refusals below name the tag; the remote chose it, so it is shown redacted.
        shown_tag = runtime._redact(tag)

        dest = worktree_path(repo)
        refusal = await loop.run_in_executor(None, _dest_refusal, repo, dest)
        if refusal is not None:
            return refusal

        # `_dest_refusal` only clears the destination PATH. A worktree already registered
        # under this basename at a DIFFERENT path owns the fleet-row identity and pod name
        # this action would claim -- the row is keyed by basename, not absolute path -- so
        # two would collide and `git worktree add` at a fresh path would not catch it. A
        # record at the reserved path ITSELF, whose directory is gone (a create killed
        # mid-checkout, a tree deleted by hand), is refused too: git would not add over it,
        # and the cleanup below force-removes a record at this path, which is only safe for
        # one this request's own `worktree add` made. Refuse here, before the network fetch,
        # with the path named. The list is read STRICT through `_run_git` like every other
        # local step, and fails closed if it cannot be read, since the name cannot be cleared.
        try:
            listed = await asyncio.to_thread(_run_git, ["worktree", "list", "--porcelain"], repo)
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot list the registered worktrees: {runtime._redact(str(exc))}",
            }
        if listed.returncode != 0:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot list the registered worktrees: {_error_detail(listed.stderr)}",
            }
        for entry in repository._parse_worktree_porcelain(listed.stdout):
            path = entry.get("path")
            if not path or os.path.basename(path) != WORKTREE_NAME:
                continue
            shown = runtime._redact(str(path))
            # `_same_path` resolves both paths: a stat per component, which blocks in the
            # kernel for as long as an NFS or FUSE mount takes to answer, so it runs off
            # the loop like the destination check above.
            if not await asyncio.to_thread(repository._same_path, path, dest):
                return {
                    "ok": False,
                    "code": "name_taken",
                    "error": (
                        f"a worktree named {WORKTREE_NAME} is already registered at "
                        f"{shown} -- remove it first, then create it again"
                    ),
                }
            clear = (
                f"unlock it (`git worktree unlock {shown}`), then run `git worktree prune`"
                if entry.get("locked")
                else "run `git worktree prune`"
            )
            return {
                "ok": False,
                "code": "name_taken",
                "error": (
                    f"{WORKTREE_NAME} is still registered at {shown}, but its directory is "
                    f"gone -- {clear} in the main checkout, then create it again"
                ),
            }

        # Refuse a repo that declares a checkout filter BEFORE fetching or touching
        # disk. The strict tier the checkout runs at already contains a filter that
        # slips in afterwards (credentials are masked there), so this is the early,
        # friendly refusal; the tier is the boundary.
        try:
            offending = await asyncio.to_thread(_checkout_filter, repo)
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot read the checkout-filter config: {runtime._redact(str(exc))}",
            }
        if offending:
            named = runtime._redact(offending)
            return {
                "ok": False,
                "code": "filter_declared",
                "error": (
                    f"this repository configures a content filter ({named}) that git "
                    "would run on checkout; create the worktree manually"
                ),
            }

        # Refuse a partial clone BEFORE fetching. Its promisor remote's filter applies
        # to the one-tag fetch too, so the release's files are not local and `worktree
        # add` would fetch them itself, from inside the strict checkout: a network step
        # outside the two `_run_network_git` bounds, and one that has no git credentials
        # there. Like the filter probe this is an early, friendly refusal; a promisor
        # planted afterwards meets the strict tier, which is the boundary.
        try:
            promisor = await asyncio.to_thread(_run_git, ["config", "--list", "-z"], repo)
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot read the clone's promisor config: {runtime._redact(str(exc))}",
            }
        # `--list` exits 0 whatever keys are set, so any other exit fails closed.
        if promisor.returncode != 0:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot read the clone's promisor config: {_error_detail(promisor.stderr)}",
            }
        if _promisor_in_force(promisor.stdout):
            return {
                "ok": False,
                "code": "partial_clone",
                "error": (
                    "the main checkout is a partial clone (a promisor remote is configured), "
                    "so the release's files would be fetched during the checkout itself; "
                    "create the worktree manually, or hydrate this clone by running in it "
                    "`git config --unset remote.<name>.partialclonefilter`, then "
                    "`git fetch --refetch <name>`, then "
                    "`git config --unset remote.<name>.promisor` (and "
                    "`git config --unset extensions.partialclone` if it is set)"
                ),
            }

        remote = await repository._upstream_remote()
        # Fetch ONLY this tag's objects, once, and let no config turn that one fetch into
        # a ref write, a second fetch elsewhere, or work that outlives it:
        #   --no-tags, --refmap=   write no `refs/tags/` entry and ignore a configured
        #       `remote.<name>.fetch`, so neither the tag nor a mapped ref lands;
        #   --no-recurse-submodules  a `submodule.recurse` / `fetch.recurseSubmodules`
        #       (on-demand is git's default) would otherwise run a second `git fetch`
        #       inside a populated submodule -- a fetch in another repository;
        #   -c fetch.bundleURI=    a configured `fetch.bundleURI` would otherwise pull
        #       from a bundle before the fetch and write `refs/bundles/*`;
        #   --no-auto-maintenance  a fetch otherwise ends by starting
        #       `git maintenance run --auto`, which by default detaches and keeps
        #       working on the shared `.git` after this request has answered.
        # --no-write-fetch-head records no FETCH_HEAD. The objects land in the store and
        # the detached checkout below references them.
        try:
            rc, _out, ferr = await _run_network_git(
                [
                    "git",
                    "-C",
                    repo,
                    "-c",
                    "fetch.bundleURI=",
                    "fetch",
                    "--no-auto-maintenance",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--refmap=",
                    "--no-recurse-submodules",
                    remote,
                    f"refs/tags/{tag}",
                ],
                mode=mode,
                timeout=120,
            )
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        if rc != 0:
            return {
                "ok": False,
                "code": "fetch_failed",
                "error": (
                    f"cannot fetch {shown_tag}: {_error_detail(ferr)}" + _private_remote_hint(mode)
                ),
            }

        # The advertised commit must now be present as a commit locally. If the
        # remote served a different object than it advertised, or the tag moved
        # under the fetch, the oid is absent and this refuses rather than checking
        # out something the remote did not publish. Local, strict, no network.
        try:
            present = await asyncio.to_thread(
                _run_git,
                [_NO_REPLACE, "rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}"],
                repo,
            )
        except SandboxUnavailable as exc:
            return _sandbox_refusal(str(exc))
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "code": "git_failed",
                "error": f"cannot check the fetched commit: {runtime._redact(str(exc))}",
            }
        if present.returncode != 0 or present.stdout.strip() != oid:
            return {
                "ok": False,
                "code": "provenance_mismatch",
                "error": f"the remote did not provide the advertised commit for {shown_tag}",
            }

        # Claim, check out, verify and, on failure, clean up as ONE transaction that a
        # cancellation cannot cut short. A gateway restart or shutdown cancels in-flight
        # handlers, and `asyncio.to_thread` does not stop its worker: without this the
        # coroutine would unwind, releasing `_CREATE_LOCK`, while `git worktree add` is
        # still writing, and skip the readback and the cleanup, leaving a claimed
        # directory or an unverified tree behind. `_run_uninterruptible` holds this frame,
        # and with it the lock, until the transaction settles, then re-raises the
        # cancellation; each step in it is bounded by `_run_git`'s own timeout, so the
        # drain ends. It is the pattern the worktree removal uses for the same reason.
        refusal = await runtime._run_uninterruptible(_claim_and_check_out(repo, dest, oid))
        if refusal is not None:
            return refusal

    return {
        "ok": True,
        "path": dest,
        "name": WORKTREE_NAME,
        "version": resolved["version"],
        "ref": f"refs/tags/{tag}",
        "oid": oid,
    }


__all__ = (
    "WORKTREE_NAME",
    "advertised_release_commit",
    "create_release_worktree",
    "is_release_tag",
    "worktree_path",
)

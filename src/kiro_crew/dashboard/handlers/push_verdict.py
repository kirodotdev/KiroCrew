"""``POST /api/push-verdict/run`` — the gateway side of the prepare-pr push gate.

WHAT THIS ROUTE IS FOR
----------------------
The agent cannot be the one that records "the push guard passed": evidence the gated party
writes is not evidence. So the agent's MCP tool only PRESENTS a request here, and this
handler — running in the gateway process, the same process as the publish floor — performs
the stale-base check itself and records what IT observed via
:func:`kiro_crew.security.push_verdict.record`.

MACHINE-only (loopback + ``X-Internal-Secret``), listed in
``server._STRICT_INTERNAL_API_PATHS``. No browser calls it. Both checks are re-asserted
here for the reason ``handlers/computer_use.py`` states about its own legs: being listed in
that frozenset does NOT prove the secret was checked, because with the header absent the
middleware falls through to cookie auth, and a ``local_only=False`` deployment reclassifies
strict paths as mixed.

THE TARGET COMES FROM THE CALLER'S IDENTITY, NEVER ITS BODY
-----------------------------------------------------------
Which worktree the guard runs against is read from the CALLING SESSION's slot, the contract
``handlers/session_ledger.py`` states for its own routes. If the body could name the
worktree, an agent could ask for a verdict about a clean tree and then publish from a
different one — and the verdict would be true, about the wrong repository. The request body
carries nothing this handler trusts.

WHY THE GATEWAY RUNS THE REAL GUARD RATHER THAN ITS OWN CHECK
-------------------------------------------------------------
A verdict that says "the push guard passed" has to mean the push guard passed, so this
handler executes the actual ``prepare-pr`` script,
``builtin_skills/kirocrew-dev/prepare-pr/scripts/push_guard.py``, and records a verdict only
on its exit 0. It deliberately does NOT re-implement the check: that script runs four
(fetch, ancestry, ahead-count, and patch-id replay detection), and a second copy here would
be both a weaker verdict than it claims and a grammar that drifts from the one the skill
actually documents.

Two properties of that invocation are load-bearing.

It runs the PACKAGED copy under ``kiro_crew/builtin_skills/``, never the copy installed into
the user's skills directory. The installed copy is agent-writable, so running it would hand
the gated party the check that authorizes it — the same defect one layer out.

It PINS the base rather than letting the script auto-detect, so the base recorded in the
verdict is exactly the base that was judged. The base is the repository's own default branch
from ``refs/remotes/origin/HEAD``, NOT the current branch's upstream: a feature branch
already pushed once has its own remote branch as upstream, and judging staleness against
that is trivially satisfied — a check that always passes.

The script fetches, which is a network call, and that is correct HERE: this is a tool call
the agent awaits, not the permission gate. The gate itself reads process memory only.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.origin import is_loopback
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import is_incognito_transcript
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)
from kiro_crew.security import push_verdict
from kiro_crew.sel import sel
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

logger = logging.getLogger(__name__)

OP_RUN = "push_verdict.run"

#: Bound on each git call. A worktree on a stalled mount must not hold the request open
#: indefinitely; the agent gets a refusal it can act on instead of a hang.
_GIT_TIMEOUT_SECONDS = 20

#: The guard itself fetches, so it gets a longer bound than the local rev-parse reads.
_GUARD_TIMEOUT_SECONDS = 120

#: The gateway's own push. Longer than a guard run because this one moves objects over the
#: network, and a publish that times out halfway is worse than one that waits.
_PUSH_TIMEOUT_SECONDS = 300

#: The PACKAGED guard. Resolved through ``skills._BUILTIN_SKILLS_DIR`` rather than a second
#: copy of that path expression, so the two cannot drift, and deliberately not through the
#: user's installed skills directory: that copy is agent-writable, and running it would let
#: the gated party supply the check that authorizes it.
_PUSH_GUARD = _BUILTIN_SKILLS_DIR / "kirocrew-dev" / "prepare-pr" / "scripts" / "push_guard.py"

#: Git configuration that can EXECUTE a program, neutralized for every git this module
#: launches.
#:
#: This is load-bearing because the worktree being judged is AGENT-WRITABLE while this code
#: runs in the GATEWAY process, outside the agent's sandbox. An agent that writes
#: ``core.sshCommand`` into its own ``.git/config`` would otherwise have the gateway run that
#: command for it, at gateway privilege, simply by asking for a verdict.
#:
#: Passed as ENVIRONMENT configuration rather than ``-c`` arguments for one reason that
#: matters: ``GIT_CONFIG_COUNT`` entries have the highest precedence git offers AND they are
#: inherited, so they also govern the git processes the guard script starts for itself. A
#: ``-c`` on this module's own command line would protect only this module's own calls.
#:
#: It is a DENYLIST and is honestly described as one: a future git release can add another
#: executable directive, and git offers no switch that ignores repository configuration. What
#: it closes is every directive git documents today as running a program. The complete answer
#: is to run the guard inside a sandbox with the worktree mounted read-only, which is a
#: larger change than this module.
_NEUTRALIZED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.sshCommand", "ssh"),
    ("core.gitProxy", ""),
    ("core.fsmonitor", "false"),
    ("core.askPass", ""),
    ("core.pager", "cat"),
    ("core.hooksPath", "/dev/null"),
    ("core.alternateRefsCommand", ""),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("protocol.ext.allow", "never"),
    ("uploadpack.packObjectsHook", ""),
)


def git_env() -> dict[str, str]:
    """The environment every git this module launches runs under.

    Also clears ``GIT_TERMINAL_PROMPT`` so a credential prompt inside the gateway becomes a
    failed fetch the caller is told about, rather than a request blocking on a terminal no
    one is attached to.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = str(len(_NEUTRALIZED_GIT_CONFIG))
    for index, (key, value) in enumerate(_NEUTRALIZED_GIT_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


#: What the guard's own contract means, kept here so the caller reads intent not integers.
_GUARD_SAFE = 0
_GUARD_REFUSED = 40

#: The refs the gateway fetches into its own mirror and points the guard at. Namespaced under
#: ``refs/kirocrew/`` so they cannot collide with anything a repository already has, and under
#: a PER-REQUEST token so two requests cannot collide with each other.
_REF_PREFIX = "refs/kirocrew/push-verdict"


@dataclass(frozen=True)
class _JudgementRefs:
    """One request's private pair of refs inside the mirror.

    Fixed ref names were a correctness bug, not an aesthetic one: one mirror serves one
    REPOSITORY, so two sessions judging two branches of it at once wrote the same two refs.
    The second fetch replaced the first's candidate, and the guard then measured one session's
    branch and recorded the result for the other's. A token per request makes the two runs
    invisible to each other, and the refs are deleted afterwards so the mirror does not
    accumulate one pair per judgement forever.
    """

    base: str
    candidate: str

    @classmethod
    def mint(cls) -> _JudgementRefs:
        token = uuid.uuid4().hex
        return cls(base=f"{_REF_PREFIX}/{token}/base", candidate=f"{_REF_PREFIX}/{token}/candidate")


@dataclass(frozen=True)
class _GuardRun:
    """What one guard run observed, carried together so the caller cannot mix runs.

    ``head`` and ``base_sha`` are resolved from the refs the guard was POINTED AT, inside the
    mirror, not read from the worktree afterwards. Re-reading the worktree's ``HEAD`` was its
    own gap: a commit landing between the fetch and that read produced a verdict recording a
    commit the guard never examined, which is the verdict describing a tree it did not judge.
    """

    rc: int
    output: str
    head: str
    base_sha: str
    #: The mirror and the refs this run fetched into it, kept ALIVE for the caller. Deleting them
    #: here was right while the gateway only judged, and wrong the moment it also publishes: the
    #: push source must be the ref holding the exact commit the guard examined, so the refs have
    #: to outlive the judging and be removed by whoever finishes the operation. ``None`` only on
    #: a failure that never minted them.
    mirror: Path | None = None
    refs: _JudgementRefs | None = None


async def _prepare_sandboxed_spawn(
    argv: list[str], *, env: dict[str, str], visible: tuple[str, ...]
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare the sandbox for one spawn, on a worker thread.

    Routes through ``sandboxed_spawn_argv``, this repository's single chokepoint for an
    agent-influenced spawn, by way of the shared off-loop owner. Modeled on
    ``kiro_prerequisite._prepare_sandboxed_spawn``, which ``test/test_spawn_audit.py`` pins as
    the shape an async caller of the chokepoint must keep.

    Routing is the load-bearing mitigation here, not a formality. The command runs against an
    AGENT-WRITABLE repository, and git reads executable directives out of that repository's own
    configuration, so the child must be confined even when it does something the agent chose:
    the sandbox hides the credential directories and hands the child a scrubbed environment, so
    a directive that does execute cannot reach what the gateway can reach.

    ``mode="standard"`` rather than ``"strict"`` because the guard FETCHES: standard is the
    mode documented as hiding non-workflow credential directories while leaving git-over-SSH
    usable. ``visible`` names the directories this spawn must be able to see -- the worktree it
    reads and, for the mirror operations, the gateway's own repository -- so a mask over any
    hidden parent does not also hide them.
    """
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            mode="standard",
            env=env,
            extra_visible_dirs=visible,
        )
    )


async def _run_git(
    argv: list[str], *, visible: tuple[str, ...], timeout: int, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """The single routed spawn for every git this module runs.

    One spawn site rather than one per caller: the repository's spawn audit reads the ENCLOSING
    function, so a second site would need the routing argued for twice, and this way a caller
    cannot add a git call that quietly skips the sandbox.
    """
    wrapped, scrubbed, cleanup = await _prepare_sandboxed_spawn(
        argv, env=env or git_env(), visible=visible
    )
    try:
        proc = await create_subprocess_limited(
            *wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=scrubbed,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timed out"
        return proc.returncode or 0, stdout.decode("utf-8", "replace").strip()
    finally:
        # The chokepoint materializes a launcher or profile the CALLER must remove.
        if cleanup:
            with contextlib.suppress(OSError):
                os.unlink(cleanup)


async def _git(worktree: str, *args: str) -> tuple[int, str]:
    """READ something out of *worktree*.

    ``-C`` rather than a chdir: this coroutine runs in the gateway's event loop, which is
    shared, and a process-wide working-directory change would race every other task.

    Every use of this helper is a read. The worktree is never written, which is why the judging
    happens in the gateway's own mirror instead.
    """
    return await _run_git(
        ["git", "-C", worktree, *args], visible=(worktree,), timeout=_GIT_TIMEOUT_SECONDS
    )


def _mirror_for(gitdir: str) -> Path:
    """The gateway's own bare repository for the repository at *gitdir*.

    Keyed on a digest of the git directory rather than on a branch or a remote URL: one
    repository gets one mirror however many branches or worktrees it has, two repositories never
    collide, and the name carries no path fragment an operator might read as a location.
    """
    digest = hashlib.sha256(gitdir.encode("utf-8")).hexdigest()[:32]
    return data_home() / push_verdict.MIRROR_DIR / f"{digest}.git"


async def _prime_mirror(
    worktree: str, mirror: Path, base: str, url: str, refs: _JudgementRefs
) -> tuple[int, str]:
    """Put the base and the candidate into the gateway's mirror, reading the worktree only.

    Two fetches, and the direction of each is the point. The BASE comes from the remote, so it
    is the fresh tip rather than whatever the worktree last saw. The CANDIDATE is fetched OUT of
    the worktree, which reads it and writes only into the mirror -- the reason a read-only
    worktree can be judged at all, since a fetch INTO one fails on its own ``FETCH_HEAD``.
    """
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not (mirror / "HEAD").exists():
        rc, out = await _run_git(
            ["git", "init", "--quiet", "--bare", str(mirror)],
            visible=(str(mirror.parent),),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return rc, f"could not create the gateway's mirror: {out}"

    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "fetch", "--quiet", url, f"+{base}:{refs.base}"],
        visible=(str(mirror.parent),),
        timeout=_GUARD_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return rc, f"could not fetch {base} from the remote: {out}"

    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "fetch", "--quiet", worktree, f"+HEAD:{refs.candidate}"],
        visible=(worktree, str(mirror.parent)),
        timeout=_GUARD_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return rc, f"could not read this branch out of the worktree: {out}"
    return 0, ""


class _GuardUntrusted(RuntimeError):
    """No copy of the push guard can be shown to be the one an operator authorized.

    Separate from an ``OSError`` because the remedy differs: an I/O failure is a machine
    problem, while this is an operator action -- pin or re-pin the digest -- and the message
    has to say which.
    """


async def _resolve(mirror: Path, ref: str) -> str:
    """The commit *ref* names inside *mirror*, or ``""`` when it names none.

    ``^{commit}`` so a ref pointing at a tag or a tree answers nothing rather than answering
    an object the guard's ancestry check cannot mean.
    """
    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        visible=(str(mirror.parent),),
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return out if rc == 0 else ""


@dataclass(frozen=True)
class _PushTarget:
    """Where a publish from one branch actually lands, and why it could not be established.

    ONE resolver, read twice: the route resolves the target before judging, and the publish
    re-resolves it immediately before pushing. A second copy of this precedence would be the
    defect this file has already paid for twice -- two spellings of one rule that drift, and the
    one that drifts is the one that decides. ``code`` empty means resolved.
    """

    remote: str
    url: str
    code: str = ""
    detail: str = ""


async def _effective_push_target(worktree: str, source_ref: str) -> _PushTarget:
    """The remote a publish from *source_ref* REACHES, in git's own precedence.

    Assuming ``origin`` was a real gap: with ``branch.<name>.pushRemote`` or
    ``remote.pushDefault`` set, a bare ``git push`` names no remote, so nothing could be
    compared while the commit landed in a repository the guard never looked at.

    A remote that FETCHES from one repository and PUSHES to another (``remote.<name>.pushurl``)
    is refused rather than resolved: the gateway judges the tree it can read, and if the publish
    lands elsewhere the judgement describes the wrong repository under an accepted remote name.
    """
    remote = ""
    for key in (f"branch.{source_ref}.pushRemote" if source_ref else "", "remote.pushDefault"):
        if not key:
            continue
        rc, value = await _git(worktree, "config", "--get", key)
        if rc == 0 and value:
            remote = value
            break
    if not remote and source_ref:
        rc, value = await _git(worktree, "config", "--get", f"branch.{source_ref}.remote")
        remote = value if rc == 0 and value else ""
    remote = remote or "origin"

    rc, fetch_url = await _git(worktree, "remote", "get-url", remote)
    rc_push, push_url = await _git(worktree, "remote", "get-url", "--push", remote)
    if rc != 0 or not fetch_url or rc_push != 0 or not push_url:
        return _PushTarget(
            remote,
            "",
            "no_remote_url",
            f"the remote `{remote}` has no resolvable URL, so there is nothing to fetch a "
            "base from",
        )
    if push_url != fetch_url:
        return _PushTarget(
            remote,
            "",
            "push_url_differs",
            (
                f"the remote `{remote}` fetches from one repository and pushes to another "
                "(a pushurl is configured), so a verdict computed against what can be read "
                "would not describe where the publish lands. Refused rather than answered; "
                "remove the pushurl, or publish from a remote whose two URLs agree."
            ),
        )
    return _PushTarget(remote, push_url)


@dataclass(frozen=True)
class _PublishResult:
    """What the gateway's own push did. ``code`` names the outcome for the caller and the log."""

    ok: bool
    code: str
    detail: str


async def _remote_tip(mirror: Path, url: str, ref: str) -> tuple[int, str]:
    """The commit the remote has at ``refs/heads/<ref>`` right now, read BY THE GATEWAY.

    This is what the lease is taken against, and reading it here rather than accepting it from
    the caller is the whole value: a lease against a SHA the agent supplied would let the agent
    describe a remote state that never existed, which is the same defect as a receipt the agent
    writes. An absent branch answers ``""``, which is how git spells "must not exist".
    """
    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "ls-remote", url, f"refs/heads/{ref}"],
        visible=(str(mirror.parent),),
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return rc, ""
    first = out.split("\n", 1)[0].strip()
    return 0, first.split("\t", 1)[0] if first else ""


async def _publish(
    *,
    worktree: str,
    mirror: Path,
    refs: _JudgementRefs,
    head: str,
    source_ref: str,
    target: _PushTarget,
) -> _PublishResult:
    """Re-validate what was judged, then PUSH IT. The one operation that publishes.

    The gateway performing the push is what closes the gap every earlier revision left open.
    While the agent held the push, a judgement could only ever describe a PAST state: the guard
    snapshotted ``HEAD``, and a mutation the gateway never observed -- a script file it
    authorized as one opaque invocation, a non-shell interpreter, a process it did not launch --
    moved the tree before the agent's own ``git push`` ran. Nothing downstream could tell,
    because a branch push names no commit for a receipt to be compared against.

    Two properties do the work, and neither is a matcher.

    The push SOURCE is the judged commit itself, by SHA, out of the mirror ref that holds it. So
    what lands is what the guard examined, whatever the worktree says by now. Re-reading the
    worktree at push time would reopen the window in the last place it could still be opened.

    And the state the judging assumed is re-checked immediately before the push: ``HEAD`` must
    still be that commit, and the effective remote and its push URL must still be the ones
    validated. Either having moved REFUSES rather than publishes, because something moved
    underneath a judgement and the honest answer is to judge again.
    """
    rc, current = await _git(worktree, "rev-parse", "HEAD")
    if rc != 0 or not current:
        return _PublishResult(
            False, "head_unreadable", "the worktree's HEAD could not be re-read before publishing"
        )
    if current != head:
        return _PublishResult(
            False,
            "head_moved",
            (
                f"HEAD moved from the judged commit {head[:12]} to {current[:12]} after the "
                "guard ran, so nothing has judged what publishing would land now. Ask again."
            ),
        )

    # Re-resolved rather than remembered: a ``pushurl`` or ``pushRemote`` written after the
    # judging would otherwise send the judged commit to a repository the guard never read,
    # which is the destination half of the same class.
    now = await _effective_push_target(worktree, source_ref)
    if now.code:
        return _PublishResult(False, now.code, now.detail)
    if now.remote != target.remote or now.url != target.url:
        return _PublishResult(
            False,
            "target_moved",
            (
                f"the push destination changed from `{target.remote}` to `{now.remote}` after "
                "the guard ran, so the judged commit would land somewhere unjudged. Ask again."
            ),
        )

    rc, tip = await _remote_tip(mirror, target.url, source_ref)
    if rc != 0:
        return _PublishResult(
            False,
            "remote_unreadable",
            f"the remote's current {source_ref} could not be read, so no lease can be taken",
        )

    # ``--force-with-lease`` against the tip THIS gateway just read. A plain push would refuse
    # every rebase, which is the workflow the guard exists to serve, and a bare ``--force``
    # would discard whatever arrived meanwhile. The lease makes a concurrent remote update fail
    # closed instead, and it is honest because the expected value is the gateway's own reading.
    env = git_env()
    env["GIT_DIR"] = str(mirror)
    rc, output = await _run_git(
        [
            "git",
            "--git-dir",
            str(mirror),
            "push",
            f"--force-with-lease=refs/heads/{source_ref}:{tip}",
            target.url,
            f"{refs.candidate}:refs/heads/{source_ref}",
        ],
        visible=(str(mirror.parent),),
        timeout=_PUSH_TIMEOUT_SECONDS,
        env=env,
    )
    if rc != 0:
        return _PublishResult(False, "push_failed", output[-2000:])
    return _PublishResult(True, "published", output[-2000:])


async def _delete_refs(mirror: Path, refs: _JudgementRefs) -> None:
    """Remove one judgement's refs from the mirror. Best effort: a leftover ref costs space."""
    for ref in (refs.base, refs.candidate):
        await _run_git(
            ["git", "--git-dir", str(mirror), "update-ref", "-d", ref],
            visible=(str(mirror.parent),),
            timeout=_GIT_TIMEOUT_SECONDS,
        )


def _guard_snapshot(digest: str) -> Path:
    """The gateway's copy of the guard, verified against the keystone's pinned *digest*.

    The packaged guard lives inside the installation tree, and in an editable or source
    install that tree is the repository the AGENT edits. Executing it there hands the gated
    party the script that authorizes it: edit the file, always print ``SAFE TO PUSH``, and
    the gateway records a verdict for a push nobody checked. That is the same defect this
    design removes one layer out, so it cannot be left standing one layer in.

    Two things together answer it, and neither is sufficient alone.

    The PIN is the trust anchor. ``digest`` comes from the activation keystone, which only an
    operator can write, so it is the one statement about these bytes that does not come from
    inside the tree being vouched for. Bytes that do not match it are refused, not run. A
    digest computed here and remembered could only ever say "the same as last time", and last
    time was also inside the agent's reach.

    The SNAPSHOT is what executes. It lives in the sealed directory, the same seal as the
    mirror and the activation leaf, so nothing can swap the file between the moment its
    digest was checked and the moment the interpreter opens it.

    The snapshot is refreshed whenever it does not match the pin, which is safe precisely
    because the pin -- not the copy's age -- is what authorizes the bytes. That is what a
    once-only copy could not do: it made first use the authority, so an edit landing before
    any verdict was ever requested became the authorized version for good.
    """
    if not digest:
        raise _GuardUntrusted(
            "push-verdict gating is activated on this installation but no guard digest is "
            "pinned in " + push_verdict.ACTIVATION_LEAF + ", so no copy of the push guard can "
            "be shown to be the one an operator authorized. An operator pins guard_sha256 "
            "there; the agent cannot write that path, which is the whole reason the pin is "
            "worth anything."
        )
    source = _PUSH_GUARD.read_bytes()
    actual = hashlib.sha256(source).hexdigest()
    if actual != digest:
        raise _GuardUntrusted(
            f"the installed push guard hashes to {actual[:12]} while the keystone pins "
            f"{digest[:12]}, so these are not the bytes an operator authorized. Refused "
            "rather than run: in a source install this file is the repository the agent "
            "edits, and the pin is the only thing outside that repository able to vouch for "
            "it. An operator re-pins guard_sha256 after a deliberate guard change."
        )
    snapshot = data_home() / push_verdict.MIRROR_DIR / "push_guard.py"
    if not snapshot.exists() or hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        # tmp+rename so a crash mid-copy cannot leave a truncated script that would fail
        # open-ish: a half-written guard exits non-zero, which refuses rather than passes,
        # but a torn file is still not something to execute.
        tmp = snapshot.with_suffix(".py.tmp")
        tmp.write_bytes(source)
        tmp.replace(snapshot)
    return snapshot


async def _run_guard(worktree: str, base: str, *, gitdir: str, digest: str, url: str) -> _GuardRun:
    """Judge *worktree* WITHOUT writing to it, and return what the guard observed.

    The worktree is read; everything written goes into the gateway's own mirror. That split is
    what makes the verdict trustworthy in both directions at once: the tree being judged cannot
    be changed by the judging, and the agent cannot reach the repository the judgement is
    computed in.

    The guard itself is the gateway's own SNAPSHOT of the packaged script, in out-of-place mode,
    so there is exactly one copy of the four checks -- fetch freshness, ancestry, ahead-count and
    patch-id replay -- rather than a second copy here that would drift from the one the skill
    documents, and so an agent editing the repository copy cannot change what runs.

    ``sys.executable`` because the guard is stdlib-only and must run under the interpreter the
    gateway runs under, not whatever ``python3`` a PATH happens to resolve.
    """
    try:
        guard = _guard_snapshot(digest)
    except _GuardUntrusted as exc:
        # No copy the keystone vouches for means no trustworthy verdict. Refusing costs a
        # push; running unvouched bytes would cost the guarantee the verdict is supposed to be.
        return _GuardRun(2, str(exc), "", "")
    except OSError as exc:
        return _GuardRun(
            2, f"the gateway could not establish its own copy of the push guard ({exc})", "", ""
        )

    mirror = _mirror_for(gitdir)
    refs = _JudgementRefs.mint()
    try:
        rc, detail = await _prime_mirror(worktree, mirror, base, url, refs)
        if rc != 0:
            return _GuardRun(2, detail, "", "", mirror, refs)

        # Resolved from the refs the guard is about to be pointed at, INSIDE the mirror. These
        # are the only two commits it can examine, so they are the only two a verdict may
        # claim. Reading the worktree's ``HEAD`` again after the run would read whatever has
        # landed since, and record a commit nothing checked.
        head = await _resolve(mirror, refs.candidate)
        base_sha = await _resolve(mirror, refs.base)
        if not head or not base_sha:
            return _GuardRun(
                2,
                "the gateway's mirror could not resolve the pair it fetched",
                "",
                "",
                mirror,
                refs,
            )

        env = git_env()
        env["GIT_DIR"] = str(mirror)
        rc, output = await _run_git(
            [
                sys.executable,
                str(guard),
                "--base",
                base,
                "--no-fetch",
                "--base-ref",
                refs.base,
                "--candidate-ref",
                refs.candidate,
            ],
            visible=(worktree, str(mirror.parent)),
            timeout=_GUARD_TIMEOUT_SECONDS,
            env=env,
        )
        return _GuardRun(rc, output, head, base_sha, mirror, refs)
    except BaseException:
        # Only an ABNORMAL exit cleans up here. Every ordinary return hands the refs to the
        # caller, which owns them through the publish and removes them in its own ``finally``
        # -- one owner per request either way, so a concurrent judgement never has its pair
        # deleted underneath it.
        await _delete_refs(mirror, refs)
        raise


async def api_push_verdict_run(request: web.Request) -> web.Response:
    """Run the stale-base check for the calling session and record the verdict."""
    state: DashboardState = request.app["state"]

    # AUTHORIZATION FIRST, before anything else is parsed.
    if not is_loopback(request.remote or ""):
        sel().log_api_access(
            caller="",
            operation=OP_RUN,
            outcome="denied",
            source="loopback",
            resources="non-loopback",
            error="loopback only",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)

    if request.get("internal_auth") is not True:
        sel().log_api_access(
            caller="",
            operation=OP_RUN,
            outcome="denied",
            source="loopback",
            resources=request.path,
            error="internal secret required",
        )
        return web.json_response(
            {"error": "forbidden", "code": "internal_secret_required"}, status=403
        )

    session_key = (request.headers.get("X-Session-Key") or "").strip()
    if not session_key:
        return web.json_response(
            {"error": "a session identity is required", "code": "session_required"}, status=400
        )

    # The same recognition gate the ledger routes use: an unrecognised session, a restricted
    # mode, or a channel-namespace mismatch is refused here rather than inside the check.
    refusal = await _recognize_session(
        state,
        session_key,
        OP_RUN,
        blocks_persisted_mode=is_incognito_transcript,
    )
    if refusal is not None:
        return refusal

    slot = state.get_slot(_normalize_slot_key(session_key))
    worktree = (getattr(slot, "project", "") or "").strip() if slot is not None else ""
    if not worktree:
        return web.json_response(
            {
                "error": (
                    "this session has no project directory, so there is no worktree to "
                    "judge. Set one and ask again."
                ),
                "code": "no_project",
            },
            status=400,
        )

    rc, gitdir = await _git(worktree, "rev-parse", "--absolute-git-dir")
    if rc != 0 or not gitdir:
        return web.json_response({"error": "not a git worktree", "code": "not_a_repo"}, status=400)

    # The branch HEAD points at, or "" on a detached HEAD. Read FIRST because the remote a
    # publish actually lands in is per-branch configurable. Recorded so the floor can hold a
    # publish to the ref that was judged: a pass earned here does not describe another branch.
    rc, source_ref = await _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0:
        source_ref = ""

    target = await _effective_push_target(worktree, source_ref)
    if target.code:
        return web.json_response({"error": target.detail, "code": target.code}, status=400)
    remote, push_url = target.remote, target.url

    # The repository's own default branch on THAT remote, NOT this branch's upstream. A feature
    # branch that has been pushed once has its own remote branch as upstream, and staleness
    # measured against that is satisfied by construction -- a check that can never fail.
    rc, remote_head = await _git(worktree, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
    base = remote_head.split("/", 1)[1] if rc == 0 and "/" in remote_head else ""
    if not base:
        return web.json_response(
            {
                "error": (
                    f"the default branch could not be resolved from refs/remotes/{remote}/HEAD, "
                    "so there is no base to judge against"
                ),
                "code": "no_base",
            },
            status=400,
        )

    # Only the keystone authorizes this gate, and it is also where the guard's digest is
    # pinned. Read once, here, so the digest handed to the runner and the enable the floor
    # honours come from the same read of the same file.
    try:
        activation = push_verdict.activation()
    except push_verdict.ActivationUnreadable as exc:
        return web.json_response(
            {
                "error": (
                    "the activation record exists but could not be read, so whether this "
                    f"installation gates publishes is unknown ({exc})"
                ),
                "code": "activation_unreadable",
            },
            status=500,
        )
    if not activation.enabled:
        # Nothing to record: with gating off the floor never consults a verdict, and recording
        # one would bank a pass for an installation that never asked to be gated.
        return web.json_response(
            {
                "verdict": "not_activated",
                "base": base,
                "detail": (
                    "push-verdict gating is not activated on this installation, so there is "
                    "no verdict to record and publishes are not gated on one."
                ),
            }
        )

    # The guard itself, on its own contract: 0 SAFE, 40 REFUSED, anything else an
    # environment failure. Every non-zero path records NOTHING, so a failure to judge is
    # indistinguishable from never having asked -- which the floor denies.
    run = await _run_guard(
        worktree, base, gitdir=gitdir, digest=activation.guard_sha256, url=push_url
    )
    try:
        return await _finish_operation(
            run,
            session_key=session_key,
            worktree=worktree,
            gitdir=gitdir,
            base=base,
            target=target,
            source_ref=source_ref,
        )
    finally:
        # One owner for the judgement's refs, and it is whoever finishes the operation. They
        # have to survive the publish, because the push source is the candidate ref itself, and
        # the mirror is long-lived so a pair left behind per judgement would accumulate forever.
        if run.mirror is not None and run.refs is not None:
            await _delete_refs(run.mirror, run.refs)


async def _finish_operation(
    run: _GuardRun,
    *,
    session_key: str,
    worktree: str,
    gitdir: str,
    base: str,
    target: _PushTarget,
    source_ref: str,
) -> web.Response:
    """Audit, record, PUBLISH, and release -- the half of the operation after the guard.

    Split from the route only so the refs have exactly one owner: the caller wraps this in the
    ``finally`` that removes them, which a flat route could not do without repeating the cleanup
    at every exit.

    The receipt is consumed on EVERY path out of here, which is what "no reusable publish
    authority" means operationally. While a receipt outlived its operation, a session that had
    once been judged held standing authority to publish again -- and that is the authority the
    agent must not have, because the only thing that can honestly spend it is this operation.
    An agent's own ``git push`` therefore finds no receipt and is refused by the floor.
    """
    if run.rc != _GUARD_SAFE:
        sel().log_api_access(
            caller=session_key,
            operation=OP_RUN,
            outcome="refused" if run.rc == _GUARD_REFUSED else "guard_error",
            source="loopback",
            resources=worktree,
            error=f"push_guard exit {run.rc}",
        )
        return web.json_response(
            {
                "verdict": "refused" if run.rc == _GUARD_REFUSED else "error",
                "base": base,
                "exit_code": run.rc,
                "detail": run.output[-2000:],
            }
        )
    if run.mirror is None or run.refs is None:
        # Unreachable on the guard's own contract -- a pass implies a primed mirror -- and
        # refused rather than asserted, because publishing needs the ref that holds the judged
        # commit and there is nothing safe to push without it.
        return web.json_response(
            {"error": "the guard passed without a mirror to publish from", "code": "no_mirror"},
            status=500,
        )

    # The pair the guard EXAMINED, resolved inside the mirror from the refs it was pointed at.
    # Not a fresh read of the worktree: a commit landing between the fetch and such a read
    # would be recorded as judged when nothing had judged it.
    head, base_sha = run.head, run.base_sha

    # A verdict this session already holds for a DIFFERENT repository is dropped HERE, before
    # the audit, because by this point the route has established which tree the session is in
    # and the old pass is wrong from that moment. Placing it after the audit left the stale
    # verdict standing on every path that refuses, which is the opposite of what a refusal
    # should cost. This is also what READS the recorded ``gitdir``.
    previous = push_verdict.verdict_for(session_key)
    if previous is not None and previous.gitdir != gitdir:
        push_verdict.invalidate(session_key)

    # The audit record and the effect are ONE transaction, and the ORDER is the whole point:
    # a recorded verdict that no audit describes is an unaudited pass, which is exactly the
    # failure class this design exists to remove. So the audit is written FIRST and a failure
    # to write it refuses the request without recording anything. The reverse order would
    # leave a pass standing whose only trace failed to be written; an audit with no record is
    # harmless by comparison, because the floor denies on an absent verdict.
    try:
        # `critical=True` is the audit-or-deny contract: the event is written synchronously
        # and a filesystem failure is RE-RAISED. Without it this helper enqueues and returns
        # success even when the write fails, so the ordering below would be decoration --
        # the except branch could never run and an unaudited pass would still be recorded.
        # Synchronous means off-loop: `asyncio.to_thread` keeps the event loop free, which
        # is how `cron.py` writes a record that must land before its promoting write.
        await asyncio.to_thread(
            sel().log_api_access,
            caller=session_key,
            operation=OP_RUN,
            outcome="recorded",
            source="loopback",
            resources=worktree,
            critical=True,
        )
    except Exception:
        logger.exception("push verdict audit could not be written; recording nothing")
        return web.json_response(
            {
                "error": (
                    "the verdict could not be audited, so it was not recorded. Nothing is "
                    "gated differently; ask again once auditing works."
                ),
                "code": "audit_failed",
            },
            status=500,
        )

    # Only now, having run the guard here AND audited it, does the gateway record its own
    # observation. The values come from THIS run; nothing in the request body reaches the
    # store. The record is bound to the operation below and released with it.
    push_verdict.record(
        session_key,
        gitdir=gitdir,
        worktree=worktree,
        head=head,
        base=base,
        base_sha=base_sha,
        remote=target.remote,
        source_ref=source_ref,
    )
    try:
        published = await _publish(
            worktree=worktree,
            mirror=run.mirror,
            refs=run.refs,
            head=head,
            source_ref=source_ref,
            target=target,
        )
    finally:
        # Released here, on success and on every failure alike. A receipt that survived its
        # operation would be exactly the reusable authority this design removes, and the
        # failure paths are the ones that matter most: a publish refused because HEAD moved
        # must not leave behind a pass the next command could spend.
        push_verdict.invalidate(session_key)

    if not published.ok:
        sel().log_api_access(
            caller=session_key,
            operation=OP_RUN,
            outcome=published.code,
            source="loopback",
            resources=worktree,
            error=published.detail[:500],
        )
        return web.json_response(
            {
                "verdict": "not_published",
                "code": published.code,
                "head": head,
                "base": base,
                "detail": published.detail,
            },
            status=409 if published.code in ("head_moved", "target_moved") else 500,
        )

    return web.json_response(
        {
            "verdict": "published",
            "head": head,
            "base": base,
            "base_sha": base_sha,
            "remote": target.remote,
            "source_ref": source_ref,
            "detail": published.detail,
        }
    )

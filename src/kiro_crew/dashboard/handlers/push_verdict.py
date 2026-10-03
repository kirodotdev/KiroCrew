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
read from the remote's ADVERTISED symbolic HEAD (``git ls-remote --symref``), NOT the current
branch's upstream and NOT the agent-writable local ``refs/remotes/origin/HEAD``: a feature
branch already pushed once has its own remote branch as upstream, and judging staleness against
that is trivially satisfied -- a check that always passes.

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
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from kiro_crew import platform_compat
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
from kiro_crew.security import push_verdict, redact_credentials, redact_exfiltration_urls
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

#: Cap on how much of a git child's output the GATEWAY buffers. ``communicate()`` would read the
#: child's whole stream into the parent process unbounded, and the remote URL is resolved from
#: distrusted worktree config, so a hostile or broken remote's huge response would OOM the
#: gateway even though the child's own rlimits and the timeout are unaffected by output size. A
#: mebibyte holds any legitimate git progress or error text; past it the read stops, the child
#: is killed, and a truncated refusal is returned.
_MAX_GIT_OUTPUT_BYTES = 1 << 20
#: Upper bound on waiting for a KILLED git child to be reaped. A SIGKILL'd process is collected
#: promptly; this only keeps a wedged child from holding a timed-out or cancelled request open.
_REAP_TIMEOUT_SECONDS = 5

#: Cap on the guard file the gateway will read whole. The packaged guard is a small stdlib
#: script; in a source install its file is the agent-writable tree, so a read past this cap
#: fails closed (an enlarged guard cannot match the pinned digest anyway).
_MAX_GUARD_BYTES = 1 << 20


def _redacted_output(text: str, *, limit: int = 2000) -> str:
    """The tail of *text* with credential-shaped tokens and exfiltration URLs redacted.

    Guard/fetch output can carry a remote's own text -- a credential-shaped string or an
    exfiltration URL echoed back by ``git fetch``/``git push`` -- and this module returns it as
    the ``detail`` an MCP presenter shows the agent. External text into the model context is
    exactly what the redactors exist to scrub, so both are applied (the two-call pattern used by
    ``agent_panel``/``transcribe``): strip exfiltration URLs, then credentials.

    Redaction runs over the WHOLE string and the clip to *limit* chars is taken AFTER it: a
    slice taken first can cut a secret in two across the boundary -- or drop the label/prefix a
    scanner anchors on -- leaving the surviving fragment unmatched in the returned detail. The
    clip is a tail (``[-limit:]``) because a push failure's actionable text is at the end. Empty
    in, empty out.
    """
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text[-limit:]


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
    # ``-F none`` so the gateway's ssh ignores the user AND system ssh config: pinning
    # ``core.sshCommand`` to a bare ``ssh`` still let it read ``~/.ssh/config``, and that file
    # is agent-writable, so a planted ``Host *`` block (``ProxyCommand``/``LocalCommand``/
    # ``IdentityFile``/``Match exec``) would run a chosen program or key on this gateway publish
    # spawn -- which is exempt from the agent credential mask precisely so it CAN authenticate.
    # Per ``ssh_config(5)`` ``-F none`` disables reading any configuration file, so no planted
    # ssh config is honored; the gateway still authenticates from its own keys/agent as before.
    ("core.sshCommand", "ssh -F none"),
    ("core.gitProxy", ""),
    ("core.fsmonitor", "false"),
    ("core.askPass", ""),
    ("core.pager", "cat"),
    ("core.hooksPath", "/dev/null"),
    ("core.alternateRefsCommand", ""),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("protocol.ext.allow", "never"),
    # Remote-transport allowlist, not just the ``ext::`` block above. The remote URL is read
    # from the AGENT-WRITABLE worktree config, so its transport is agent-chosen: naming a
    # ``git-remote-<helper>`` transport makes git spawn ``git-remote-<helper>`` resolved
    # through the child's ``PATH``. ``protocol.ext.allow=never`` closes only ``ext::``; a
    # bare ``protocol.allow=never`` default closes EVERY transport, and the two lines below
    # re-open only the ones a gateway publish legitimately uses -- ``https``/``ssh`` for the
    # remote, and ``file`` because the gateway fetches the base into its own local mirror. Any
    # other ``git-remote-<name>`` the agent names is now refused by git before it is
    # PATH-looked-up, so no planted helper is reachable regardless of PATH. These are git's
    # real knobs (``protocol.allow`` + ``protocol.<name>.allow``, mirrored by
    # ``GIT_ALLOW_PROTOCOL``), highest-precedence via ``GIT_CONFIG_COUNT`` so they also govern
    # the git processes the guard starts for itself.
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("protocol.file.allow", "always"),
    ("uploadpack.packObjectsHook", ""),
)


def git_env() -> dict[str, str]:
    """The environment every git this module launches runs under.

    Also clears ``GIT_TERMINAL_PROMPT`` so a credential prompt inside the gateway becomes a
    failed fetch the caller is told about, rather than a request blocking on a terminal no
    one is attached to.

    ``PATH`` is rewritten to a TRUSTED-ONLY value for the git spawn, not merely led with the
    trusted git's directory. Resolving argv[0] to a trusted absolute path (in ``_run_git``)
    closes the hijack of the git binary itself, but git in turn shells out to helpers --
    ``git-remote-https``, credential helpers, ``ssh`` -- which it resolves through the CHILD's
    ``PATH``. A gateway's ``PATH`` may lead with agent-writable directories (``~/.local/bin``, a
    worktree venv's ``bin``) that the sandbox's pinned-cwd screen does not drop because they are
    not beneath the worktree, so a planted ``git-remote-<transport>`` shim on any RETAINED entry
    would win that lookup and run on this same ``gateway_publish`` spawn that holds the publish
    credentials -- the same reachable hole one level down. Retaining the remainder and only
    prepending the trusted dir was not enough: the transport is agent-chosen (the remote URL is
    read from the agent-writable worktree config), and a helper the trusted dir does not carry
    still falls through to the agent-writable entries. So the git spawn's ``PATH`` is set to the
    trusted git's own directory (where a real install keeps its helpers) FOLLOWED BY the fixed
    trusted system directories only; the agent-writable remainder is dropped. Combined with the
    ``protocol.allow`` transport allowlist, a helper for an unlisted transport is refused before
    any lookup, and a helper for a listed one resolves only from trusted directories. If no
    trusted git is resolvable the ``PATH`` is left unchanged; the spawn is refused before this
    env is used anyway (``_run_git`` returns rc 127 on a ``None`` git).
    """
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = str(len(_NEUTRALIZED_GIT_CONFIG))
    for index, (key, value) in enumerate(_NEUTRALIZED_GIT_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    # Cut the agent-writable global and system config out of EVERY gateway git. The remote
    # URL and the destination pin are both read from the worktree, and ``_effective_push_target``
    # validates the pin against them -- but a ``url.<evil>.pushInsteadOf=<pin>`` rewrite planted
    # in ``~/.gitconfig`` (or a system config) is applied by git when ``_publish`` actually runs
    # the push, AFTER that validation passed, redirecting the credentialed publish to a repo the
    # operator never pinned. ``HOME`` is not scrubbed on the gateway's own spawn, so the only
    # closure is to deny git the user/system scopes outright: ``GIT_CONFIG_GLOBAL`` pointed at the
    # null device and ``GIT_CONFIG_NOSYSTEM=1`` leave only the per-invocation ``GIT_CONFIG_*`` knobs
    # above and the worktree-local config the pin was validated against, so no out-of-tree
    # ``url.*`` rewrite can reach a gateway git after the destination was checked.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    git_bin = platform_compat.trusted_git_bin()
    if git_bin is not None:
        git_dir = os.path.dirname(git_bin)
        # Trusted-only PATH: the trusted git's own dir first (its helpers live beside it), then
        # the fixed trusted system dirs. Dedup while preserving order, and DROP the
        # agent-writable remainder so no planted helper is reachable on the credentialed spawn.
        trusted_dirs = [git_dir, *platform_compat._TRUSTED_SYSTEM_BIN_DIRS]
        seen: set[str] = set()
        ordered: list[str] = []
        for directory in trusted_dirs:
            if directory and directory not in seen:
                seen.add(directory)
                ordered.append(directory)
        env["PATH"] = os.pathsep.join(ordered)
    return env


#: What the guard's own contract means, kept here so the caller reads intent not integers.
_GUARD_SAFE = 0
_GUARD_REFUSED = 40

#: The refs the gateway fetches into its own mirror and points the guard at. Namespaced under
#: ``refs/kirocrew/`` so they cannot collide with anything a repository already has, and under
#: a PER-REQUEST token so two requests cannot collide with each other.
_REF_PREFIX = "refs/kirocrew/push-verdict"

#: The branches a gateway publish must never TARGET directly, mirroring the argv floor's own
#: set (``main``/``mainline``/``master``) rather than importing its private symbol so the two  # wokeignore:rule=master
#: cannot be coupled: a feature-branch workflow publishes to a feature branch, and a push
#: whose source ref is one of these -- or is empty, a detached HEAD -- names a destination the
#: gate was never meant to feed. Held as its own copy here because the floor's constant is a
#: private detail of a different module, and a security floor that silently followed another
#: module's edits would be one edit away from being widened.
_PROTECTED_PUBLISH_BRANCHES = {"main", "mainline", "master"}  # wokeignore:rule=master


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
    argv: list[str],
    *,
    env: dict[str, str],
    visible: tuple[str, ...],
    writable: tuple[str, ...] = (),
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

    ``gateway_publish=True`` marks this as the one gateway-owned git that KEEPS ``~/.ssh`` even
    when push-verdict gating is activated. On an activated install every agent-influenced spawn
    loses the SSH key so an opaque subprocess cannot push past the argv floor; this path is the
    single operation trusted to publish, so it is exempt from that mask and authenticates over
    SSH as before. Withholding the key here instead would break the gateway's own fetch and push.

    ``writable`` names the directories this gateway-owned spawn must be able to WRITE. The
    gateway's own mirror leaf lives under ``push-verdict-mirrors``, which is a crew-home
    readonly leaf the sandbox seals against every agent subprocess -- so ``git init --bare`` and
    the mirror fetches would fail EROFS and no verdict could ever be issued. This is the one
    trusted spawn that OWNS that mirror, so its leaf is passed as ``extra_writable_dirs``, a
    scoped write carve-out INSIDE the readonly guard (the chokepoint's subtree validator admits
    it); ``extra_visible_dirs`` only cancels HIDDEN masks and cannot make a readonly subtree
    writable, which is why a separate writable carve is required.
    """
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            mode="standard",
            env=env,
            gateway_publish=True,
            extra_visible_dirs=visible,
            extra_writable_dirs=writable,
        )
    )


async def _read_capped(proc: asyncio.subprocess.Process, cap: int) -> tuple[int, bytes]:
    """Drain ``proc.stdout`` into memory but never past ``cap`` bytes.

    ``communicate()`` buffers the child's whole stream in the parent unbounded, and this
    module's git children resolve their remote from distrusted worktree config, so a hostile
    remote's huge response would exhaust the gateway. Read in chunks and stop at the cap: if the
    child exceeds it, kill the child and return a nonzero rc with a truncation notice rather than
    keep buffering. A child that finishes under the cap returns its own exit status and output,
    so the common small-output path is unchanged in shape.
    """
    assert proc.stdout is not None
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return 1, f"git output exceeded {cap} bytes and was truncated".encode()
    rc = await proc.wait()
    return rc, b"".join(chunks)


async def _kill_and_reap(proc: object) -> None:
    """Kill *proc* and reap it under a short bound, so a stuck child cannot hold the request.

    A killed child is normally reaped at once, but ``wait()`` is unbounded: a child wedged in
    uninterruptible state would otherwise let a timed-out or CANCELLED request hang on the reap
    -- the very thing the kill exists to prevent. Bounding the wait means the request always
    makes progress; a rare unreaped child is the OS's to collect, not this request's to wait on.
    Every failure mode (no ``kill``/``wait``, kill raising, the reap timing out) is swallowed.
    """
    with contextlib.suppress(Exception):
        proc.kill()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)  # type: ignore[attr-defined]


async def _run_git(
    argv: list[str],
    *,
    visible: tuple[str, ...],
    timeout: int,
    env: dict[str, str] | None = None,
    writable: tuple[str, ...] = (),
) -> tuple[int, str]:
    """The single routed spawn for every program this module runs.

    One spawn site rather than one per caller: the repository's spawn audit reads the ENCLOSING
    function, so a second site would need the routing argued for twice, and this way a caller
    cannot add a spawn that quietly skips the sandbox. Two shapes of ``argv[0]`` route through
    here, and NEITHER is ever resolved through the child's ambient ``PATH`` -- that invariant is
    what closes the hijack this function exists to close:

    * ``"git"`` -- the slash-less token every git call passes. It is resolved to a trusted
      absolute git through :func:`kiro_crew.platform_compat.trusted_git_bin`. A slash-less
      argv[0] would otherwise be resolved through the child's ``PATH``, and a gateway's ``PATH``
      may lead with agent-writable directories (a worktree venv's ``bin``, ``~/.local/bin``) --
      so a planted ``git`` shim would run here, and this is the one spawn the sandbox marks
      ``gateway_publish=True``, exempting it from the SSH + HTTPS credential masks so it can
      authenticate the publish. A shim on that spawn runs at gateway privilege holding the
      publish credentials, with no recovery once a key is read. ``trusted_git_bin()`` returns an
      absolute git off a fixed system directory (never an agent-writable one) or ``None``;
      ``None`` means "do not spawn git", so we REFUSE with a non-zero rc rather than fall back to
      bare ``"git"``, which would reinstate the hazard.

    * any other program -- ``_run_guard`` routes the packaged guard through here as
      ``[sys.executable, ...]``. CPython gives us ``sys.executable`` as an ABSOLUTE path, so it
      is spawned as passed; there is nothing to resolve and nothing hits the ambient ``PATH``.
      A non-"git" argv[0] that is NOT absolute is refused: it would fall to a ``PATH`` lookup,
      the exact hazard the "git" branch closes, so no caller may reintroduce it here.
    """
    if not argv:
        return (127, "refusing to spawn: empty argv")
    if argv[0] == "git":
        git_bin = platform_compat.trusted_git_bin()
        if git_bin is None:
            return (
                127,
                "no trusted git binary on this machine; "
                "refusing to resolve 'git' through an agent-writable PATH",
            )
        argv = [git_bin, *argv[1:]]
    elif not os.path.isabs(argv[0]):
        # A non-"git" program (the guard's ``sys.executable``) must arrive absolute so it is
        # spawned directly; a slash-less non-"git" argv[0] would resolve through the ambient
        # PATH -- the same hijack the "git" branch closes -- so refuse rather than spawn it.
        return (
            127,
            "refusing to spawn a non-'git' program by a non-absolute name; "
            "it would resolve through an agent-writable PATH",
        )
    wrapped, scrubbed, cleanup = await _prepare_sandboxed_spawn(
        argv, env=env or git_env(), visible=visible, writable=writable
    )
    try:
        proc = await create_subprocess_limited(
            *wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=scrubbed,
        )
        try:
            rc, stdout = await asyncio.wait_for(
                _read_capped(proc, _MAX_GIT_OUTPUT_BYTES), timeout=timeout
            )
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            return 124, "timed out"
        except BaseException:
            # Cancellation (gateway shutdown, client disconnect) must NOT leave the child
            # alive: this is the ``gateway_publish=True`` git, so a surviving ``git push``
            # could update the ref AFTER the operation aborted -- past the receipt/audit
            # bookkeeping this request will now skip. Kill and REAP the child before
            # propagating, so nothing it does outlives the cancelled request.
            await _kill_and_reap(proc)
            raise
        return rc, stdout.decode("utf-8", "replace").strip()
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


def _mirror_for(common_dir: str) -> Path:
    """The gateway's own bare repository for the repository whose shared git dir is *common_dir*.

    Keyed on a digest of the COMMON git directory (``git rev-parse --git-common-dir``) rather
    than a per-worktree git dir, a branch or a remote URL: one repository gets one mirror
    however many branches or LINKED WORKTREES it has -- every ``.worktrees/*`` checkout of one
    repository reports the same common dir, where ``--absolute-git-dir`` reports each its own
    ``.git/worktrees/<name>`` and so primed a separate mirror per checkout. Two repositories
    never collide, and the name carries no path fragment an operator might read as a location.
    """
    digest = hashlib.sha256(common_dir.encode("utf-8")).hexdigest()[:32]
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

    # ``mkdir`` and the ``HEAD`` existence probe are synchronous filesystem calls, and
    # ``data_home()`` may sit on a network mount an operator pointed ``KIROCREW_HOME`` at, where
    # a stalled mount turns either into a multi-second block. This runs on the gateway event
    # loop, so a block here freezes every other task including the liveness heartbeat -- offload
    # both to a worker thread, the same async-subprocess discipline ``_run_git`` already applies
    # to every other I/O in this function.
    def _ensure_mirror_parent() -> bool:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        return (mirror / "HEAD").exists()

    mirror_exists = await asyncio.to_thread(_ensure_mirror_parent)
    if not mirror_exists:
        rc, out = await _run_git(
            ["git", "init", "--quiet", "--bare", str(mirror)],
            visible=(str(mirror.parent),),
            writable=(str(mirror.parent),),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return rc, f"could not create the gateway's mirror: {out}"

    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "fetch", "--quiet", url, f"+{base}:{refs.base}"],
        visible=(str(mirror.parent),),
        writable=(str(mirror),),
        timeout=_GUARD_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return rc, f"could not fetch {base} from the remote: {out}"

    rc, out = await _run_git(
        ["git", "--git-dir", str(mirror), "fetch", "--quiet", worktree, f"+HEAD:{refs.candidate}"],
        visible=(worktree, str(mirror.parent)),
        writable=(str(mirror),),
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


def _file_url_local_path(push_url: str) -> str | None:
    """The local filesystem path a ``file:`` URL names, or ``None`` when *push_url* is not one.

    git treats ``file:///path`` and ``file://host/path`` as a LOCAL repository, not a network
    remote -- so a ``file:`` pin must get the same treatment as a bare local path (anchored,
    canonicalized, symlink-refused), NOT the pass-through a real network scheme gets. Without
    this, a ``file:`` spelling of a local destination slips past the canonicalization and the
    symlink refusal that exist for exactly these local destinations. A ``file://host/path`` with
    a non-local host is NOT a plain local path, so it is returned as its raw ``//host/path`` form
    and canonicalization/refusal then treat it as the unusual path it is rather than silently
    dropping the host. Percent-escapes are decoded so the on-disk identity is the real one.
    """
    parts = urllib.parse.urlsplit(push_url)
    if parts.scheme != "file":
        return None
    path = urllib.parse.unquote(parts.path)
    netloc = parts.netloc
    if netloc and netloc.lower() not in ("", "localhost"):
        # Preserve a non-empty, non-local authority rather than discard it: ``//host/path`` is
        # not a plain local path, and anchoring/realpath will treat it as the oddity it is.
        return f"//{netloc}{path}"
    return path or "/"


def _absolutize_local_remote(push_url: str, worktree: str) -> str:
    """A local remote path made absolute AND canonical against *worktree*, else *push_url* as-is.

    A remote URL reaches git positionally on the mirror fetches and the publish, and those
    spawns carry no ``-C worktree`` (they run ``--git-dir <mirror>``), so a git process
    resolves a relative local path against the GATEWAY's working directory, not the worktree
    the operator reasoned about. That lets a pinned relative remote name one repository at
    compare time and force-push a DIFFERENT one at publish time. A network scheme URL
    (``https://``, ``ssh://``, ``git://`` ...) and the scp-like ``user@host:path`` form are
    network/remote identities, never gateway-relative, so they are left untouched; a ``file:``
    URL is a LOCAL repository and is unwrapped to its path and anchored like a bare local path,
    and a bare local path is anchored to the worktree, so compare and use name the same
    repository.

    A local path is ALSO canonicalized with ``os.path.realpath`` so its identity is its real
    target on disk, not a lexical spelling: the destination config is agent-writable and the
    worktree is agent-writable, so a pinned path could point THROUGH an agent-planted symlink to
    a repository the operator never authorized. Canonicalizing here makes the judge-time and the
    publish-time resolutions compare the same real path (``_publish`` re-runs this and refuses
    ``target_moved`` on any drift), and the caller separately REFUSES a symlinked local
    destination outright via :func:`_local_remote_is_symlinked`.
    """
    parts = urllib.parse.urlsplit(push_url)
    file_path = _file_url_local_path(push_url)
    if file_path is not None:
        # ``file:`` names a LOCAL repository: anchor + canonicalize it like a bare local path
        # so the symlink/relative hazards below are closed for this spelling too.
        push_url = file_path
    elif parts.scheme:
        # A real network URL scheme (https/ssh/git/...): not a local filesystem path.
        return push_url
    head = push_url.split("/", 1)[0]
    if "@" in head and head.find("@") < (head.find(":") if ":" in head else len(head)):
        # scp-like ``user@host:path`` -- a remote identity, not a local path.
        return push_url
    anchored = push_url if os.path.isabs(push_url) else os.path.join(worktree, push_url)
    # realpath resolves ``..`` AND every symlink component to the real on-disk target, so the
    # identity that is compared and pushed is the canonical one an operator can reason about.
    return os.path.realpath(anchored)


def _local_remote_is_symlinked(push_url: str, worktree: str) -> bool:
    """True when *push_url* is a LOCAL path that reaches its target through a symlink.

    A network scheme URL or scp-like remote is never a local path, so it is never symlinked
    here; a ``file:`` URL IS a local repository and is unwrapped to its path and checked like a
    bare local path. For a local path, any symlink on the way to the target -- including the
    final component -- means an agent-writable link could redirect the pinned destination to a
    repository the operator did not authorize, so the caller refuses it rather than following
    it. Compares the lexical absolute anchor against its ``realpath``: if they differ, a symlink
    (or a ``..`` crossing a symlink) was traversed.
    """
    parts = urllib.parse.urlsplit(push_url)
    file_path = _file_url_local_path(push_url)
    if file_path is not None:
        push_url = file_path
    elif parts.scheme:
        return False
    head = push_url.split("/", 1)[0]
    if "@" in head and head.find("@") < (head.find(":") if ":" in head else len(head)):
        return False
    anchored = push_url if os.path.isabs(push_url) else os.path.join(worktree, push_url)
    lexical = os.path.normpath(anchored)
    return os.path.realpath(anchored) != lexical


def _is_local_destination(push_url: str) -> bool:
    """True when *push_url* names a LOCAL repository (a bare path or a ``file:`` URL).

    git treats both a bare filesystem path and a ``file:`` URL as a local repository it writes
    to directly on disk -- no network, and crucially NO credentials. The gateway's primary
    enforcement of a verdict is the credential mask it spawns the publish under, so a local
    destination escapes it entirely: an opaque subprocess with ordinary filesystem access can
    ``git push`` to a local path with no credential the mask could withhold. A real network
    scheme (``https`` / ``ssh`` / ``git`` / ...) and the scp-like ``user@host:path`` form are
    remote identities and return False. Mirrors the local/remote split ``_absolutize_local_remote``
    and ``_local_remote_is_symlinked`` already use, so the three agree on what "local" means.
    """
    if _file_url_local_path(push_url) is not None:
        return True
    parts = urllib.parse.urlsplit(push_url)
    if parts.scheme:
        # A real network URL scheme (https/ssh/git/...): not a local filesystem path.
        return False
    head = push_url.split("/", 1)[0]
    if "@" in head and head.find("@") < (head.find(":") if ":" in head else len(head)):
        # scp-like ``user@host:path`` -- a remote identity, not a local path.
        return False
    return True


async def _effective_push_target(
    worktree: str, source_ref: str, *, activated: bool = False, pinned_push_url: str = ""
) -> _PushTarget:
    """The remote a publish from *source_ref* REACHES, in git's own precedence.

    Assuming ``origin`` was a real gap: with ``branch.<name>.pushRemote`` or
    ``remote.pushDefault`` set, a bare ``git push`` names no remote, so nothing could be
    compared while the commit landed in a repository the guard never looked at.

    A remote that FETCHES from one repository and PUSHES to another (``remote.<name>.pushurl``)
    is refused rather than resolved: the gateway judges the tree it can read, and if the publish
    lands elsewhere the judgement describes the wrong repository under an accepted remote name.

    ``pinned_push_url`` is the operator's DESTINATION pin, carried on the activation keystone
    the agent cannot write. Every URL resolved here comes from the AGENT-WRITABLE worktree git
    config -- ``remote.<name>.url`` and the ``branch.*.pushRemote`` / ``remote.pushDefault``
    that select the remote -- so an ordinary ``git remote set-url`` before the request would
    otherwise point the credential-exempt gateway publish at an attacker-chosen same-history
    repository, and the publish-time re-resolution only compares the value to itself. When an
    operator has pinned a destination, a resolved push URL that does not match it is REFUSED
    (``unpinned_destination``) HERE, at the single resolver every git argv the URL flows into
    passes through.

    ``activated`` is whether push-verdict gating is enabled on this installation. On an
    ACTIVATED install with NO pin the publish is REFUSED (``destination_not_pinned``): the push
    destination is read from the agent-writable worktree config, so leaving it unconstrained on
    a gated install is the fail-OPEN this finding closes -- the credential-exempt gateway
    publish would land the judged commit at any agent-chosen same-history repository, and that
    is the ORDINARY unbounded path, not a rarity. Activation therefore REQUIRES the operator to
    pin a destination in order to publish at all. A NON-activated install is not gated and its
    destination stays unconstrained exactly as before -- the requirement is the consequence of
    activating, never a default imposed on an install that never asked to be gated.
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
    if push_url.startswith("-"):
        # A remote URL is read verbatim from the agent-writable worktree config and then reaches
        # git as a positional argument on the credential-exempt gateway publish spawn. git reads a
        # leading-dash token as an OPTION, not a URL, so a value like ``--upload-pack=<path>`` runs
        # a chosen program at gateway privilege. A real remote URL is a scheme, an ``ssh``-style
        # ``user@host:path`` or a filesystem path -- none begin with ``-`` -- so a leading dash is
        # refused at this single resolver return, which covers every git argv the URL flows into.
        return _PushTarget(
            remote,
            "",
            "unsafe_remote_url",
            (
                f"the remote `{remote}` resolves to a URL beginning with `-`, which git reads as an "
                "option rather than a repository. Refused rather than published."
            ),
        )
    if push_verdict._url_has_unparseable_port(push_url):
        # A remote URL is read verbatim from the agent-writable worktree config. A non-numeric
        # or out-of-range port (``host:notaport``, ``host:99999``) makes ``urlsplit(...).port``
        # raise ValueError, which the credential-free compare and the pin resolution both read,
        # so an unhandled crash would take down the push request. Refuse it at the SAME single
        # resolver return the other URL refusals sit at (covers pre-judgement resolve AND the
        # pre-push re-resolve) rather than crashing -- a real destination has a parseable port.
        return _PushTarget(
            remote,
            "",
            "unsafe_remote_url",
            (
                f"the remote `{remote}` resolves to a URL whose port cannot be parsed, which git "
                "cannot use as a repository. Refused rather than published."
            ),
        )
    if activated and not pinned_push_url:
        # Fail-CLOSED on an activated install that pinned no destination, at the SAME single
        # resolver return the other URL refusals sit at, so it covers the route's
        # pre-judgement resolve AND the publish-time re-resolve with one check. The push URL is
        # resolved from agent-writable worktree config, so leaving it unconstrained on a gated
        # install lets the credential-exempt gateway publish land the judged commit at any
        # agent-chosen same-history repository -- the fail-OPEN this finding closes, and the
        # ORDINARY unbounded path rather than a rarity. So an activated install now REQUIRES the
        # operator to pin a destination before it can publish at all. A NON-activated install
        # reaches here with ``activated`` False and is unaffected -- it is not gated, so its
        # destination stays unconstrained exactly as before.
        return _PushTarget(
            remote,
            "",
            "destination_not_pinned",
            (
                "push-verdict gating is activated on this installation but no push destination "
                "is pinned, so the credential-backed gateway publish would send the judged "
                "commit to whatever destination the agent-writable worktree git config names. "
                "An activated installation must pin the destination it authorizes: set "
                "`pinned_push_url` on the push-verdict activation keystone to the repository "
                "publishes may target, then ask again. Refused rather than published."
            ),
        )
    if pinned_push_url and push_verdict._credential_free_url(
        push_url
    ) != push_verdict._credential_free_url(pinned_push_url):
        # The destination pin, enforced at the SAME single resolver return the other URL
        # refusals sit at, so it covers the route's pre-judgement resolve AND the publish-time
        # re-resolve with one check. The push URL is resolved from agent-writable worktree
        # config; the pin is on the operator-only keystone. A mismatch means the agent picked a
        # destination other than the one the operator authorized -- the finding this closes --
        # so it is refused rather than published. Both sides are compared on a CREDENTIAL-FREE
        # identity (``_credential_free_url`` strips any ``user[:secret]@`` userinfo): the pin is
        # refused at read time if it carries an embedded credential, and a token in the resolved
        # ``git remote get-url`` names the same repository, so a credential must neither defeat
        # the match nor be required in the pin. Whitespace was already stripped from the pin
        # where it is read; host and path are preserved, so a genuinely different repo still
        # mismatches.
        return _PushTarget(
            remote,
            "",
            "unpinned_destination",
            (
                f"the remote `{remote}` resolves to a push URL that is not the destination an "
                "operator pinned for this installation, so publishing would send the judged "
                "commit to a repository the operator did not authorize. The push destination "
                "is read from the worktree git config, which the agent can rewrite, so it is "
                "bound to the operator-pinned identity on the activation keystone the agent "
                "cannot write. Publish to the pinned destination, or have an operator re-pin "
                "pinned_push_url."
            ),
        )
    if _local_remote_is_symlinked(push_url, worktree):
        # A local destination reached THROUGH a symlink is refused, not followed: the push URL
        # comes from the agent-writable worktree config and the worktree tree is agent-writable,
        # so a planted link could redirect the pinned/resolved destination to a repository the
        # operator never authorized. Refused at the SAME single resolver return the other URL
        # refusals sit at, so it covers the route's pre-judgement resolve AND ``_publish``'s
        # re-resolve immediately before pushing -- the "revalidate canonical identity before
        # pushing" half. A genuine on-disk repository directory is not a symlink and passes.
        return _PushTarget(
            remote,
            "",
            "unsafe_remote_url",
            (
                f"the remote `{remote}` resolves to a local path that reaches its target "
                "through a symlink, which an agent could repoint to a repository the operator "
                "did not authorize. Refused rather than followed; pin a real (non-symlinked) "
                "destination path."
            ),
        )
    if activated and _is_local_destination(push_url):
        # A local destination (bare path or ``file:`` URL) is a repository git writes to on disk
        # with NO credentials, so the gateway's credential mask -- its one lever for enforcing a
        # verdict on the publish -- cannot bind it: an opaque subprocess with ordinary filesystem
        # access publishes to it directly, judged by nothing. On an ACTIVATED install that is a
        # fail-OPEN, so refuse it here at the single resolver return (covering both the
        # pre-judgement resolve and ``_publish``'s re-resolve) rather than canonicalize and push
        # it. A NON-activated install is not gated and keeps a local remote exactly as before.
        return _PushTarget(
            remote,
            "",
            "unsafe_remote_url",
            (
                f"the remote `{remote}` resolves to a LOCAL repository (a filesystem path or a "
                "`file:` URL), which git publishes to with no credentials -- so the gateway's "
                "credential mask cannot bind the publish and the push-verdict gate cannot be "
                "enforced on it. Refused on an activated install; publish to a network remote "
                "(https/ssh) whose credentialed push the gateway owns."
            ),
        )
    push_url = _absolutize_local_remote(push_url, worktree)
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
    base: str,
    base_sha: str,
    target: _PushTarget,
    activated: bool = False,
    pinned_push_url: str = "",
) -> _PublishResult:
    """Re-validate what was judged, then PUSH IT. The one operation that publishes.

    The gateway performing the push is what closes the gap between judgement and publish.
    An agent that holds the push can only ever have a judgement describe a PAST state: the guard
    snapshotted ``HEAD``, and a mutation the gateway never observed -- a script file it
    authorized as one opaque invocation, a non-shell interpreter, a process it did not launch --
    moved the tree before the agent's own ``git push`` ran. Nothing downstream could tell,
    because a branch push names no commit for a receipt to be compared against.

    Two properties do the work, and neither is a matcher.

    The push SOURCE is the judged commit itself, by SHA, out of the mirror ref that holds it. So
    what lands is what the guard examined, whatever the worktree says by now. Re-reading the
    worktree at push time would reopen the window in the last place it could still be opened.

    And the state the judging assumed is re-checked immediately before the push: ``HEAD`` must
    still be that commit, the effective remote and its push URL must still be the ones
    validated, and the BASE branch tip must still be the ``base_sha`` the guard measured
    staleness against. Any of them having moved REFUSES rather than publishes, because
    something moved underneath a judgement and the honest answer is to judge again.
    """
    # The source ref names WHERE the judged commit would land, and two shapes must never
    # publish. A detached HEAD (``source_ref == ""``) has no branch to gate against, so the
    # floor could hold no verdict to the ref that was judged. A protected branch -- or a source
    # ref that IS the base being judged against -- would push the candidate onto the very
    # branch staleness is measured from, which the feature-branch workflow this gate serves
    # never does; the argv floor already refuses the agent's own such push, and the gateway
    # must not become the way around it.
    if not source_ref:
        return _PublishResult(
            False,
            "detached_head",
            "the worktree is on a detached HEAD, so there is no branch to publish and no ref "
            "the floor could gate a verdict to. Check out a branch and ask again.",
        )
    if source_ref.lower() in _PROTECTED_PUBLISH_BRANCHES or source_ref == base:
        return _PublishResult(
            False,
            "protected_source_ref",
            (
                f"publishing would push onto `{source_ref}`, which is a protected branch or the "
                "very base being judged against, and this gate only ever publishes a feature "
                "branch. Refused rather than published."
            ),
        )

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
    # which is the destination half of the same class. The operator pin is re-applied here
    # too, so a destination the operator did not authorize is refused (``unpinned_destination``)
    # on the re-resolve as well as on the pre-judgement resolve; and on an activated install
    # with no pin the same fail-closed ``destination_not_pinned`` refusal re-applies here.
    now = await _effective_push_target(
        worktree, source_ref, activated=activated, pinned_push_url=pinned_push_url
    )
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

    # The BASE branch tip the guard measured staleness against, re-read HERE from the remote.
    # A base that advances after the judging means the candidate was judged stale against a tip
    # that is gone, so a pass earned against it does not describe the current repository -- the
    # same window as HEAD or the target moving, on the other side of the comparison. Refuse and
    # judge again rather than publish a candidate whose staleness was decided against a gone tip.
    rc, base_now = await _remote_tip(mirror, target.url, base)
    if rc != 0:
        return _PublishResult(
            False,
            "base_unreadable",
            f"the remote's current {base} could not be read, so it cannot be confirmed the "
            "candidate was judged against the base that is live now",
        )
    if base_now != base_sha:
        return _PublishResult(
            False,
            "base_moved",
            (
                f"the base `{base}` advanced from the judged tip {base_sha[:12]} to "
                f"{base_now[:12] or '(deleted)'} after the guard ran, so the candidate's "
                "staleness was decided against a tip that is gone. Ask again."
            ),
        )

    rc, tip = await _remote_tip(mirror, target.url, source_ref)
    if rc != 0:
        return _PublishResult(
            False,
            "remote_unreadable",
            f"the remote's current {source_ref} could not be read, so no lease can be taken",
        )

    # A fresh lease against the tip THIS gateway just read matches even when the candidate does
    # NOT descend from that tip: the lease only asserts the remote is where the gateway saw it,
    # not that the push fast-forwards, so a candidate that diverged from a commit a collaborator
    # pushed to the shared source_ref would degrade the lease to a plain force and overwrite the
    # unseen commit. Refuse unless the remote tip is an ancestor of the candidate. An empty tip
    # is a new branch with nothing to overwrite, so the check is skipped. The candidate lives in
    # the mirror as refs.candidate; the remote tip must be present too for merge-base to see it,
    # so fetch it first and treat an unfetchable tip as diverged (refuse rather than force past).
    if tip:
        rc, _ = await _run_git(
            ["git", "--git-dir", str(mirror), "fetch", "--quiet", target.url, tip],
            visible=(str(mirror.parent),),
            writable=(str(mirror),),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return _PublishResult(
                False,
                "source_diverged",
                (
                    f"the remote `{source_ref}` has commit {tip[:12]} that could not be fetched "
                    "to confirm the judged candidate builds on it, so publishing might force past "
                    "an unseen commit. Ask again after integrating it."
                ),
            )
        rc, _ = await _run_git(
            ["git", "--git-dir", str(mirror), "merge-base", "--is-ancestor", tip, refs.candidate],
            visible=(str(mirror.parent),),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if rc != 0:
            return _PublishResult(
                False,
                "source_diverged",
                (
                    f"the remote `{source_ref}` has commit {tip[:12]} that the judged candidate "
                    "does not build on, so publishing would force past an unseen commit. Ask "
                    "again after integrating it."
                ),
            )

    # ``--force-with-lease`` against the tip THIS gateway just read. A plain push would refuse
    # every rebase, which is the workflow the guard exists to serve, and a bare ``--force``
    # would discard whatever arrived meanwhile. The lease makes a concurrent remote update fail
    # closed instead, and it is honest because the expected value is the gateway's own reading.
    #
    # The base ref is NOT leased here: ``git push`` enforces ``--force-with-lease`` only for a
    # ref the push actually UPDATES, and this push writes the feature ref alone, so a lease on
    # the un-pushed base ref is silently ignored and would be false protection. The base is
    # instead checked by the ``base_now`` read above, re-read as late as this function can place
    # it before the handshake. Residual, stated rather than hidden: the default branch can still
    # advance in the window between that read and this push, and plain ``git push`` has no
    # client-side primitive to bind a ref it does not update -- closing it needs a server-side
    # atomic compare-and-swap the generic remote does not expose. A protected or base-equal
    # source ref was already refused above.
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
        return _PublishResult(False, "push_failed", _redacted_output(output))
    return _PublishResult(True, "published", _redacted_output(output))


async def _delete_refs(mirror: Path, refs: _JudgementRefs) -> None:
    """Remove one judgement's refs from the mirror. Best effort: a leftover ref costs space."""
    for ref in (refs.base, refs.candidate):
        await _run_git(
            ["git", "--git-dir", str(mirror), "update-ref", "-d", ref],
            visible=(str(mirror.parent),),
            writable=(str(mirror),),
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
    # Bound the read: the packaged guard is a small stdlib script, but in a source install the
    # file is the agent-writable tree, so an enlarged file must fail CLOSED (refused) rather than
    # be read whole into the gateway. A guard over the cap cannot be the pinned bytes anyway
    # (its digest will not match), so a bounded read that stops past the cap is enough to refuse.
    try:
        with open(_PUSH_GUARD, "rb") as fh:
            source = fh.read(_MAX_GUARD_BYTES + 1)
    except OSError:
        raise
    if len(source) > _MAX_GUARD_BYTES:
        raise _GuardUntrusted(
            f"the installed push guard is larger than {_MAX_GUARD_BYTES} bytes, which the "
            "pinned stdlib guard never is, so it is refused rather than read whole into the "
            "gateway. An operator re-pins guard_sha256 after a deliberate guard change."
        )
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


async def _run_guard(
    worktree: str, base: str, *, common_dir: str, digest: str, url: str
) -> _GuardRun:
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
        # Off the event loop: ``_guard_snapshot`` does a ``read_bytes`` + sha256 + ``write_bytes``
        # of the packaged guard, and in a source install that file is the agent-writable tree, so
        # its size is not bounded by us. Running it synchronously on the loop would stall the
        # gateway (or exhaust memory on an enlarged file) for every request; ``to_thread`` moves
        # the whole blocking helper off, mirroring the other off-loop reads in this file.
        guard = await asyncio.to_thread(_guard_snapshot, digest)
    except _GuardUntrusted as exc:
        # No copy the keystone vouches for means no trustworthy verdict. Refusing costs a
        # push; running unvouched bytes would cost the guarantee the verdict is supposed to be.
        return _GuardRun(2, str(exc), "", "")
    except OSError as exc:
        return _GuardRun(
            2, f"the gateway could not establish its own copy of the push guard ({exc})", "", ""
        )

    mirror = _mirror_for(common_dir)
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
                # Isolated interpreter on the credential-exempt publish spawn: ``-I`` ignores
                # ``PYTHON*`` env, the cwd and ``PYTHONPATH`` and does not add the user site dir,
                # and ``-S`` skips ``site`` initialization entirely -- so an agent-written
                # ``sitecustomize.py``/``usercustomize.py`` or a ``PYTHONPATH=src`` source-run
                # gateway cannot execute attacker code before the pinned guard. The guard is
                # stdlib-only, so it needs neither site nor any PYTHONPATH entry.
                "-I",
                "-S",
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

    # The COMMON git dir -- the one shared ``.git`` of a repository, which every linked
    # ``.worktrees/*`` checkout reports identically, where ``--absolute-git-dir`` reports each
    # worktree's own ``.git/worktrees/<name>``. The mirror is keyed on THIS so one repository
    # gets one mirror however many worktrees it has; keying the mirror on the per-worktree dir
    # primed and fetched a separate bare mirror per checkout of the same repo. ``--path-format``
    # keeps git >= 2.31 from answering a path relative to the cwd; resolve for older git too.
    rc, common_dir = await _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc != 0 or not common_dir:
        # Older git without ``--path-format``: fall back to a plain ``--git-common-dir`` resolved
        # against the worktree, and to the per-worktree dir if even that is unavailable.
        rc, common_dir = await _git(worktree, "rev-parse", "--git-common-dir")
        common_dir = (
            os.path.abspath(os.path.join(worktree, common_dir))
            if rc == 0 and common_dir
            else gitdir
        )

    # The branch HEAD points at, or "" on a detached HEAD. Read FIRST because the remote a
    # publish actually lands in is per-branch configurable. Recorded so the floor can hold a
    # publish to the ref that was judged: a pass earned here does not describe another branch.
    rc, source_ref = await _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0:
        source_ref = ""

    # Only the keystone authorizes this gate, and it is also where the guard's digest and the
    # operator-pinned push destination are recorded. Read HERE, before the destination is
    # resolved, so the pin can be enforced on the very first resolution rather than only on the
    # publish-time re-resolution -- an agent-chosen destination that was in place BEFORE the
    # request is exactly what the finding turns on, and the re-resolution alone only catches a
    # change AFTER judging. One read of the one file feeds the enable, the digest and the pin.
    try:
        # Read the keystone OFF the event loop: ``activation()`` opens and ``json.load``s the
        # activation leaf, a blocking filesystem call, and a stalled crew-home filesystem would
        # otherwise freeze every gateway task and the liveness heartbeat until it returns.
        activation = await asyncio.to_thread(push_verdict.activation)
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

    # The activation gate comes BEFORE any credentialed remote work. A non-activated install
    # is not gated at all, so it must not resolve an agent-writable destination or make a
    # credentialed remote probe against it: doing so is ordinary operation on every default
    # install and exposes the publisher's credential to an agent-chosen destination read from
    # worktree config -- reachable and unbounded. So the exit is here, before ``_effective_
    # push_target`` resolves the destination and before the ``ls-remote --symref`` probe. The
    # base is not known yet (it comes from that probe), so it is reported empty; the reader
    # tolerates an empty base and the ``not_activated`` branch never uses it. Non-remote input
    # validation stays above, unchanged; an activated install falls through and resolves,
    # pins, probes, guards and publishes in exactly the same order as before.
    if not activation.enabled:
        # Nothing to record: with gating off the floor never consults a verdict, and recording
        # one would bank a pass for an installation that never asked to be gated.
        return web.json_response(
            {
                "verdict": "not_activated",
                "base": "",
                "detail": (
                    "push-verdict gating is not activated on this installation, so there is "
                    "no verdict to record and publishes are not gated on one."
                ),
            }
        )

    # The pin only constrains an ACTIVATED install: reaching here means the install IS
    # activated, so the pin (empty when the operator pinned none) is enforced inside the
    # resolver, at the single return every git argv the URL flows into passes through -- and
    # an activated install with NO pin is fail-CLOSED there (``destination_not_pinned``),
    # because an unpinned destination on a gated install is read from the agent-writable
    # worktree config and would let the credential-exempt gateway publish reach an agent-chosen
    # repository. So ``activated`` is threaded alongside the pin, not folded into it: the empty
    # pin means two different things on the two install states, and only the flag tells them
    # apart.
    pinned_push_url = activation.pinned_push_url if activation.enabled else ""
    target = await _effective_push_target(
        worktree, source_ref, activated=activation.enabled, pinned_push_url=pinned_push_url
    )
    if target.code:
        return web.json_response({"error": target.detail, "code": target.code}, status=400)
    remote, push_url = target.remote, target.url

    # The repository's own default branch on THAT remote, NOT this branch's upstream. A feature
    # branch that has been pushed once has its own remote branch as upstream, and staleness
    # measured against that is satisfied by construction -- a check that can never fail.
    #
    # Read from the remote's ADVERTISED symbolic HEAD (``ls-remote --symref``), NOT from the
    # local ``refs/remotes/{remote}/HEAD``: that local ref is a file inside the agent-writable
    # worktree's git dir, so an agent could point it at its own already-pushed feature branch
    # and make staleness measured against the branch it is publishing -- the check-that-passes
    # this base selection exists to prevent, reintroduced one layer down. The advertised HEAD
    # comes from the remote over the wire and the agent cannot write it.
    rc, symref = await _run_git(
        ["git", "ls-remote", "--symref", push_url, "HEAD"],
        visible=(),
        timeout=_GUARD_TIMEOUT_SECONDS,
    )
    base = ""
    if rc == 0:
        for line in symref.splitlines():
            # ``ref: refs/heads/<name>\tHEAD`` is the symbolic-ref advertisement line.
            stripped = line.strip()
            if stripped.startswith("ref:") and stripped.endswith("\tHEAD"):
                ref = stripped[len("ref:") :].rsplit("\t", 1)[0].strip()
                if ref.startswith("refs/heads/"):
                    base = ref[len("refs/heads/") :]
                break
    if not base:
        return web.json_response(
            {
                "error": (
                    f"the default branch could not be resolved from the remote `{remote}`'s "
                    "advertised HEAD, so there is no base to judge against"
                ),
                "code": "no_base",
            },
            status=400,
        )

    # Only the keystone authorizes this gate, and it is also where the guard's digest is
    # pinned. Already read above (before the destination was resolved, so the enable-check
    # bailed a non-activated install before any credentialed work and the pin could be
    # enforced on the first resolution), so the digest handed to the runner and the enable the
    # floor honours come from that same single read of the same file. The enable is known
    # true here -- the non-activated exit already returned above.

    # The guard itself, on its own contract: 0 SAFE, 40 REFUSED, anything else an
    # environment failure. Every non-zero path records NOTHING, so a failure to judge is
    # indistinguishable from never having asked -- which the floor denies.
    run = await _run_guard(
        worktree, base, common_dir=common_dir, digest=activation.guard_sha256, url=push_url
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
            activated=activation.enabled,
            pinned_push_url=pinned_push_url,
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
    activated: bool = False,
    pinned_push_url: str = "",
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
                "detail": _redacted_output(run.output),
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
            lambda: sel().log_api_access(
                caller=session_key,
                operation=OP_RUN,
                outcome="recorded",
                source="loopback",
                resources=worktree,
                critical=True,
            )
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
            base=base,
            base_sha=base_sha,
            target=target,
            activated=activated,
            pinned_push_url=pinned_push_url,
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
            # A moved tree is a CONFLICT (409) and a failed push is a server error (500): the
            # status carries the HTTP-level outcome, and a committed contract pins it. The MCP
            # presenter reads the ``verdict`` field out of the body on these non-2xx statuses
            # too (see ``mcp_tools/push_verdict.py``), so the retry-guidance branch is reachable
            # without collapsing the status to 200.
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

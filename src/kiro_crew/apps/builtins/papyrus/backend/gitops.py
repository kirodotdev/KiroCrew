"""Papyrus — git operations for a paper checked out from a remote.

Papers are often already in a git repository (a hosted LaTeX service exposes one,
and lab papers live in a normal repo), so the app can clone one as a project and
push commits back. Every git invocation here follows the same three rules as
:mod:`.latex`:

* **Never on the event loop.** ``git clone`` and ``git push`` are network
  operations that can take tens of seconds; they are spawned with
  :func:`asyncio.create_subprocess_exec` and awaited under a timeout.
* **Sandbox chokepoint.** The repository is agent- and user-influenced content
  whose local hooks and config can execute code, so every spawn routes through
  :func:`kiro_crew.sandbox.sandboxed_spawn_argv` (OS isolation + scrubbed env)
  and spawns through :func:`kiro_crew.sandbox.create_subprocess_limited`.
* **No argument smuggling.** The clone URL is matched against a scheme allowlist
  and passed after ``--``, so a value like ``--upload-pack=...`` can never be
  read as an option. The project directory is produced by
  :func:`.store.safe_project_dir`, never taken from the client verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.papyrus.backend import procio
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.papyrus")

#: A clone URL must match a known transport. Modern git already refuses an
#: option-shaped URL, but the check is free and closes the door on every version.
GIT_URL_RE = re.compile(r"^(?:https?|git|ssh)://[^\s]+$|^git@[\w.-]+:[^\s]+$")

#: Wall-clock ceilings, by operation cost.
CLONE_TIMEOUT_SEC = 120.0
NETWORK_TIMEOUT_SEC = 60.0
LOCAL_TIMEOUT_SEC = 15.0

#: Default message when the client sends none.
DEFAULT_COMMIT_MESSAGE = "Update from Papyrus"

#: How many recent commits the status endpoint reports.
RECENT_COMMITS = 5

#: Stash label used by the pull autostash, so a leftover stash is identifiable.
_STASH_LABEL = "papyrus-pull-autostash"

#: Substrings that identify an authentication failure across remote types. git's
#: exit code is non-zero for every push failure and the wording varies by
#: transport, so the UI needs this to distinguish "log in" from "something broke".
_AUTH_MARKERS = (
    "authentication",
    "could not read username",
    "permission denied",
    "403 forbidden",
    "terminal prompts disabled",
)

#: Cap on the git output echoed back to the client.
MAX_OUTPUT_CHARS = 4000


class GitError(Exception):
    """A git invocation failed. ``output`` carries the (bounded) git message."""

    def __init__(self, message: str, *, output: str = "", auth: bool = False) -> None:
        super().__init__(message)
        self.output = output[:MAX_OUTPUT_CHARS]
        self.auth = auth


class GitConflict(GitError):
    """A pull could not be applied because of a real conflict."""


@dataclass
class GitStatus:
    """The toolbar's view of a project's git state."""

    is_git: bool
    branch: str = ""
    dirty: bool = False
    has_remote: bool = False
    ahead: int = 0
    behind: int = 0
    changes: list[str] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        if not self.is_git:
            return {"is_git": False}
        return {
            "is_git": True,
            "branch": self.branch,
            "dirty": self.dirty,
            "has_remote": self.has_remote,
            "ahead": self.ahead,
            "behind": self.behind,
            "changes": self.changes,
            "recent_commits": self.recent_commits,
        }


def is_git_repo(project: Path) -> bool:
    """True when *project* holds a git repository. Synchronous — offload it."""
    return (project / ".git").exists()


def git_available() -> bool:
    """True when a ``git`` binary is on PATH. Synchronous — offload it."""
    return shutil.which("git") is not None


def _audit(operation: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every git spawn. Fire-and-forget."""
    sel().log_api_access(
        caller="core:papyrus",
        operation=f"papyrus.git_{operation}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


#: Disables every ``.gitattributes``-driven program for every path.
#:
#: ``filter`` (clean/smudge/process), ``diff`` (textconv/external) and ``merge`` (custom
#: driver) each name a config key holding a COMMAND, and the repository's own
#: ``.gitattributes`` decides which paths they apply to. ``git add`` runs a clean filter,
#: so a cloned repo shipping ``* filter=x`` plus an agent-written ``filter.x.clean`` in
#: ``.git/config`` executed that command — during ``push``, i.e. in ``standard`` sandbox
#: mode with ``~/.ssh`` reachable. Verified against real git both ways.
#:
#: ``-c`` overrides cannot close this: the subsection name is ATTACKER-CHOSEN
#: (``filter.<anything>.clean``) and git does not accept a glob there — ``-c
#: 'filter.*.clean='`` was tested and the filter still ran. Unsetting the ATTRIBUTE is
#: what generalizes, because it is keyed by path rather than by the driver's name.
#:
#: ``merge=text`` rather than ``-merge``: unsetting ``merge`` makes git treat the file as
#: BINARY and declare a conflict instead of merging, which silently broke ``pull``
#: (verified — a clean 3-way merge became "could not apply"). ``text`` pins the built-in
#: 3-way driver, so a normal pull still merges while a custom driver stays unreachable.
_ATTRIBUTES_PIN = "* -filter -diff merge=text\n"


#: The remote-side program each subcommand may be told to run, and the flag that pins it.
#:
#: ``remote.<name>.uploadpack`` / ``.receivepack`` name a COMMAND, and the subsection is
#: attacker-chosen — so pinning ``remote.origin.*`` with ``-c`` covers only the remote this
#: app creates. An agent-written second remote selected via ``remote.pushDefault`` (push) or
#: ``branch.<b>.remote`` (pull) executed its own program straight past those pins; verified
#: against real git in both directions, and verified that these flags block it.
#:
#: The flag beats config for the invocation regardless of which remote is chosen, which is
#: why it is the actual guarantee rather than the config pins.
_PACK_PROGRAM_FLAGS = {
    "push": "--receive-pack=git-receive-pack",
    "pull": "--upload-pack=git-upload-pack",
    "fetch": "--upload-pack=git-upload-pack",
    "ls-remote": "--upload-pack=git-upload-pack",
    "clone": "--upload-pack=git-upload-pack",
}


def _pack_program_args(args: list[str]) -> list[str]:
    """Insert the pack-program pin right after the subcommand, when it takes one.

    Applied HERE rather than at each call site on purpose: this is the one chokepoint every
    git invocation in this module already passes through, so a subcommand added later to
    ``pull``/``push`` cannot forget it. Only the five subcommands that speak to a remote
    accept the flag — ``status``/``add``/``commit`` reject it as an unknown option, which
    would turn every local call into an error.
    """
    if not args:
        return list(args)
    flag = _PACK_PROGRAM_FLAGS.get(args[0])
    if flag is None:
        return list(args)
    # After the subcommand, before its own arguments: the flag is the subcommand's, not
    # git's, and `clone` takes positional URL/path values that must stay in order.
    return [args[0], flag, *args[1:]]


def _pin_attributes_sync(cwd: Path) -> None:
    """APPEND the pin to ``.git/info/attributes`` so no repo attribute names a program.

    ``.git/info/attributes`` is the HIGHEST-precedence attributes source — it beats the
    tree's ``.gitattributes``, including a more specific pattern in a nested directory
    (both verified). Nothing lower can be made to win, so this is the one place the pin
    holds.

    Ensured on EVERY call, deliberately. The threat model here is an agent that can write
    into the project — the same write that plants ``filter.x.clean`` in ``.git/config`` can
    delete this file, so a once-at-clone-time pin would be removable by the very actor it
    defends against.

    **Appended, never written over.** Overwriting with
    ``write_text(_ATTRIBUTES_PIN)`` would silently destroy a user's own
    ``.git/info/attributes`` — their ``text eol=lf`` and ``binary`` rules replaced by
    our one line on the next status poll, with nothing to say so. This file is
    checkout-local, so git never restores it: the loss would be permanent and invisible.

    Appending keeps the guarantee, because git resolves attributes per NAME with the LAST
    match winning — not per line. So a user rule for ``eol``/``text``/``binary`` survives
    while ``filter``/``diff``/``merge`` still end up ours, even for a pattern more specific
    than ``*``. Verified: with the user's rules kept above the pin, ``check-attr`` reports
    ``eol: lf`` and ``text: set`` alongside ``filter: unset``.

    Not a git repo yet (``clone``, whose *cwd* is the parent) → nothing to pin, and the
    clone itself is safe: filter config lives in ``.git/config``, which is created locally
    and never transferred from the remote.
    """
    git_dir = cwd / ".git"
    if not git_dir.exists():
        return
    if not git_dir.is_dir():
        # A `.git` FILE means a worktree/submodule pointing at a git dir elsewhere, so the
        # pin cannot be placed where git will read it. Papyrus projects are clones or
        # fresh inits, so this is not a shape it creates — refuse rather than run a git
        # command with attribute-driven programs live.
        raise GitError("refusing to run git: the project's .git is not a directory")
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    target = info / "attributes"
    try:
        existing = target.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        existing = ""
    # Idempotent: the pin is re-established every call, so it must not accumulate. The
    # check is for the pin as its own LINE — a substring test would match it inside a
    # comment, and a `.strip()` comparison would miss it when other rules are present.
    if _ATTRIBUTES_PIN.strip() in existing.splitlines():
        return
    # LAST, so it wins for the attributes it names. A trailing newline is added when the
    # existing content lacks one, or the pin would splice onto the user's final rule and
    # change what that line means.
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    target.write_text(prefix + _ATTRIBUTES_PIN, encoding="utf-8")


async def _git(
    args: list[str], *, cwd: Path, timeout: float = LOCAL_TIMEOUT_SEC
) -> tuple[int, str, str]:
    """Run ``git <args>`` in *cwd* off the event loop.

    Returns ``(returncode, stdout, stderr)``. A timeout kills the process tree
    and surfaces as :class:`GitError` rather than a silent empty result.
    """
    # Attribute-driven programs are disabled by a file, not by `-c` — see
    # `_pin_attributes_sync` for why the override list cannot cover them.
    await asyncio.to_thread(_pin_attributes_sync, cwd)
    # `-c` overrides BEFORE the subcommand, which is the only place git accepts them and
    # which beats anything in `.git/config`.
    #
    # A cloned repository — or the co-author agent, which can write into the project —
    # controls `.git/config`, and MANY git settings are commands git executes. Push runs
    # in `standard` sandbox mode on purpose (an SSH push needs the key), so such a command
    # would run WITH access to `~/.ssh`: arbitrary execution plus the credential the mode
    # exists to permit. Verified against real git — `core.sshCommand` set in a repo runs
    # on the next `ls-remote`, and is inert once overridden here.
    #
    # There is no "ignore repo config" switch (`GIT_CONFIG_GLOBAL`/`SYSTEM` suppress the
    # other two scopes but the repo's own is always read — checked), so this is
    # necessarily a list. It is therefore built to be BROADER than the keys this app's
    # own commands touch: the ones below cover every execution hook reachable from
    # `status`/`add`/`commit`/`pull`/`push`/`ls-remote`, which is the whole surface here.
    # A key that only affects `git send-email` or a GUI tool cannot be reached by any
    # argv this module builds.
    argv = [
        "git",
        # --- transports and remote helpers -----------------------------------------
        "-c",
        "core.sshCommand=ssh",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.allow=user",
        # These two are kept for `origin`, the remote this app creates, but they are NOT
        # the guarantee — the `--upload-pack` / `--receive-pack` FLAGS added below are.
        # `remote.<name>.*` has the same defect as `filter.<name>.*`: the name is
        # attacker-chosen, so an enumeration cannot cover it. An agent-written second
        # remote plus `remote.pushDefault` (push) or `branch.<b>.remote` (pull) selected a
        # remote whose `receivepack`/`uploadpack` pointed at a script, and it EXECUTED
        # past these pins — verified against real git in both directions.
        "-c",
        "remote.origin.uploadpack=git-upload-pack",
        "-c",
        "remote.origin.receivepack=git-receive-pack",
        "-c",
        "core.alternateRefsCommand=",
        # --- hooks that fire on ordinary porcelain ---------------------------------
        "-c",
        "core.hooksPath=/dev/null",
        # `core.fsmonitor` holds the PATHNAME OF A HOOK that `git status`/`add` run on
        # every invocation — the same class as `sshCommand`. `false` is the documented
        # "no monitor" value; an empty string would be read as a path.
        "-c",
        "core.fsmonitor=false",
        "-c",
        "gc.recentObjectsHook=",
        # --- credential and signing programs ---------------------------------------
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "gpg.program=false",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "tag.gpgSign=false",
        # --- pagers, editors and diff/merge drivers --------------------------------
        "-c",
        "core.pager=cat",
        "-c",
        "core.editor=true",
        "-c",
        "sequence.editor=true",
        "-c",
        "diff.external=",
        "-c",
        "interactive.diffFilter=",
        *_pack_program_args(args),
    ]
    # OFF the loop: `sandboxed_spawn_argv` -> `wrap_argv` -> `detect_backend` can
    # cold-probe the sandbox backend with a synchronous `subprocess.run(...,
    # timeout=5)`, and on macOS nothing warms that cache first (`prewarm_backend()`
    # returns early on non-Linux). The first push/pull of the gateway's lifetime
    # would otherwise stall the single loop — chat, cron and the liveness
    # heartbeat — for up to five seconds. Same form and reason as `latex._run` and
    # `apps/builtins/dev_fleet/server.py`.
    wrapped, env, cleanup = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), sandboxed_spawn_argv, argv
    )
    # A push/pull must never block on an interactive credential prompt: the
    # gateway has no terminal, so the child would hang until the timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # `core.gitProxy` names an EXECUTABLE git runs for `git://` remotes, so an
    # agent-written `core.gitProxy=/path/to/script` plus a `git://` remote is arbitrary
    # execution — verified against real git (the script ran and wrote its marker).
    #
    # It is fixed HERE, in the env, and not with a `-c` override, because `core.gitProxy`
    # is MULTI-VALUED: `-c` APPENDS a value rather than replacing, git uses the first
    # match, and the repo's own value is read first. Tested — `-c core.gitProxy=none`
    # (the obvious remedy), `=` and `=true` ALL still
    # executed the script, and `git -c core.gitProxy=none config --get-all core.gitProxy`
    # prints the repo's value *and* ours, which is why. `GIT_PROXY_COMMAND` is the
    # documented env equivalent and takes precedence over every config scope; verified it
    # blocks. `true` is a real no-op binary rather than an empty string, since an empty
    # value is read as a path.
    env["GIT_PROXY_COMMAND"] = "true"
    proc: asyncio.subprocess.Process | None = None
    try:
        # `create_subprocess_limited`, not `create_subprocess_exec` +
        # `preexec_fn`: a post-fork preexec forks the threaded gateway and runs
        # Python in the child before exec. The shim applies the same limits
        # AFTER exec, where the process is single-threaded.
        proc = await create_subprocess_limited(
            *wrapped,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        # `read_capped`, not `communicate()`: `git` relays sideband progress from a
        # REMOTE, so a hostile server decides how much arrives — and with a 120s timeout
        # that is a long window to write to memory at pipe speed. `MAX_OUTPUT_CHARS`
        # bounds what is DISPLAYED, not what is held. Same fix, same helper, as the
        # compiler path in `latex`.
        stdout, stderr = await asyncio.wait_for(procio.read_capped(proc), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if proc is not None and proc.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(
                    proc.pid, platform_compat.SIGKILL
                )
            except (ProcessLookupError, OSError, ValueError):
                logger.debug("papyrus: git %s already gone before kill", args[:1])
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.warning("papyrus: git did not exit after SIGKILL")
        _audit(args[0] if args else "run", str(cwd), "failure", error="timeout")
        raise GitError(f"git {args[0] if args else ''} timed out") from exc
    except FileNotFoundError as exc:
        _audit(args[0] if args else "run", str(cwd), "failure", error="git not found")
        raise GitError("git is not installed on this host") from exc
    finally:
        if cleanup:
            # Off the loop like the spawn itself. One unlink is a small syscall, but
            # the rule is about the shape, not the size: an inline syscall here is
            # what the AST guard in test_papyrus_routes.py refuses, and exempting
            # "small" ones is how the next one gets in.
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), partial(Path(cleanup).unlink, missing_ok=True)
            )
    code = proc.returncode or 0
    _audit(args[0] if args else "run", str(cwd), "ok" if code == 0 else "failure")
    return (
        code,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


def derive_project_name(url: str) -> str:
    """Derive a project name from a clone URL's last path segment."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail.lower()


async def clone(url: str, destination: Path) -> None:
    """Shallow-clone *url* into *destination*.

    *destination* must NOT exist and must already have been produced by
    :func:`.store.safe_project_dir` — this function does not validate it. The URL
    is checked against :data:`GIT_URL_RE` and passed after ``--``.

    Clones into a per-process STAGING SIBLING and renames it into place, so a failure
    only ever removes bytes this call wrote. Cloning straight into *destination* and
    cleaning it up on error had a destructive race: two concurrent clones of the same
    project name both proceed, the loser gets git's "destination path already exists"
    error, and its cleanup then deleted the WINNER's freshly-cloned checkout — turning
    a duplicate-request 500 into data loss for the request that succeeded.

    The rename is ``os.rename``, which fails rather than merging if the destination
    appeared meanwhile, so the loser reports a conflict and the winner keeps its tree.
    """
    if not GIT_URL_RE.match(url or ""):
        raise GitError("url must be http(s)://, git://, ssh://, or git@host:path")
    # Named per-process AND per-task, so two concurrent clones cannot share a staging
    # dir either. `.` prefix keeps it out of the project listing if it ever leaks.
    staging = destination.parent / f".{destination.name}.clone-{os.getpid()}-{id(destination):x}"
    try:
        code, _out, err = await _git(
            ["clone", "--depth", "1", "--", url, str(staging)],
            cwd=destination.parent,
            timeout=CLONE_TIMEOUT_SEC,
        )
    except GitError:
        await _remove_tree(staging)
        raise
    if code != 0:
        await _remove_tree(staging)
        raise GitError("git clone failed", output=err)
    try:
        # Atomic within a filesystem, and it REFUSES to clobber a non-empty directory —
        # which is what makes the loser of a race fail instead of overwriting.
        await asyncio.to_thread(os.rename, staging, destination)
    except OSError as exc:
        await _remove_tree(staging)
        raise GitError("a project with that name already exists", output=str(exc))


async def _remove_tree(path: Path) -> None:
    """Remove a partially-cloned directory off the event loop."""
    if await asyncio.to_thread(path.is_dir):
        await asyncio.to_thread(shutil.rmtree, path, True)


async def status(project: Path) -> GitStatus:
    """Collect the project's git state, or ``is_git=False`` when it is not a repo."""
    if not await asyncio.to_thread(is_git_repo, project):
        return GitStatus(is_git=False)

    _c, porcelain, _e = await _git(["status", "--porcelain"], cwd=project)
    _c, branch_out, _e = await _git(["branch", "--show-current"], cwd=project)
    _c, log_out, _e = await _git(
        ["log", f"-{RECENT_COMMITS}", "--oneline", "--no-color"], cwd=project
    )
    _c, remote_out, _e = await _git(["remote"], cwd=project)

    ahead = behind = 0
    code, counts, _e = await _git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cwd=project
    )
    if code == 0:
        parts = counts.strip().split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])

    changed = [line for line in porcelain.strip().splitlines() if line]
    return GitStatus(
        is_git=True,
        branch=branch_out.strip(),
        dirty=bool(changed),
        has_remote=bool(remote_out.strip()),
        ahead=ahead,
        behind=behind,
        changes=changed[:200],
        recent_commits=[line for line in log_out.strip().splitlines() if line],
    )


async def commit(project: Path, message: str) -> str:
    """Stage everything and commit. "Nothing to commit" is a success, not an error."""
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")
    # The `add` RESULT is checked. Discarding it meant a failed stage still went on to
    # commit, and if the index already held content — a previous partial stage, or a
    # concurrent operation — that STALE index was committed and pushed while the response
    # reported success. The user's actual edits were not in the commit, and nothing said
    # so; the next pull would then present their own missing work as a remote change.
    add_code, _add_out, add_err = await _git(["add", "-A"], cwd=project)
    if add_code != 0:
        raise GitError("git add failed", output=add_err)
    code, out, err = await _git(["commit", "-m", message or DEFAULT_COMMIT_MESSAGE], cwd=project)
    if code == 0:
        return out.strip()
    if "nothing to commit" in out.lower():
        return "nothing to commit"
    raise GitError("git commit failed", output=err or out)


async def push(project: Path) -> str:
    """Push the current branch. Raises with ``auth=True`` on a credential failure."""
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")
    code, out, err = await _git(["push"], cwd=project, timeout=NETWORK_TIMEOUT_SEC)
    if code == 0:
        return (out or err).strip()
    combined = (err + out).lower()
    if any(marker in combined for marker in _AUTH_MARKERS):
        raise GitError("authentication failed", output=err or out, auth=True)
    raise GitError("git push failed", output=err or out)


async def pull(project: Path) -> tuple[str, bool]:
    """Rebase-pull, autostashing local work. Returns ``(output, stashed)``.

    A dirty working tree — typically compiler artifacts that are not in
    ``.gitignore`` — would otherwise refuse the rebase outright, so uncommitted
    work (including untracked files) is stashed first and popped afterwards. On a
    real conflict the rebase is aborted and the stash restored, so the tree comes
    back exactly as it was. If the pop itself conflicts, the stash is deliberately
    LEFT in place — silently discarding the user's edits to finish an operation
    would be the worse outcome — and that is reported.

    Every failure path after the stash restores it, including the ones that raise
    from inside ``_git`` itself (a network pull that exceeds
    ``NETWORK_TIMEOUT_SEC``, or ``git`` disappearing mid-operation). Without that,
    a timed-out pull would return the user to an apparently-clean tree with their
    work parked in a stash they were never told about — which reads as "my edits
    vanished", the exact outcome the docstring above promises cannot happen.
    """
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")

    _c, porcelain, _e = await _git(["status", "--porcelain"], cwd=project)
    stashed = False
    if porcelain.strip():
        code, out, err = await _git(
            ["stash", "push", "--include-untracked", "-m", _STASH_LABEL], cwd=project
        )
        stashed = code == 0 and "no local changes" not in (out + err).lower()

    try:
        code, out, err = await _git(
            ["pull", "--rebase"], cwd=project, timeout=NETWORK_TIMEOUT_SEC
        )
    except GitError:
        # The pull never produced an exit code (timeout / git vanished). Put the
        # tree back before surfacing the error; a best-effort pop, because failing
        # to restore must not mask the original cause.
        if stashed:
            try:
                await _git(["stash", "pop"], cwd=project)
            except GitError:  # pragma: no cover - defensive
                logger.warning(
                    "papyrus: could not restore the autostash after a failed pull; "
                    "it is kept as '%s'",
                    _STASH_LABEL,
                )
        raise

    if code != 0:
        combined = out + err
        if "CONFLICT" in combined:
            await _git(["rebase", "--abort"], cwd=project)
            if stashed:
                await _git(["stash", "pop"], cwd=project)
            raise GitConflict("pull conflicts with upstream", output=combined)
        if stashed:
            await _git(["stash", "pop"], cwd=project)
        raise GitError("git pull failed", output=err or out)

    if stashed:
        pop_code, pop_out, pop_err = await _git(["stash", "pop"], cwd=project)
        if pop_code != 0:
            raise GitConflict(
                "pulled, but your local changes conflict with upstream — the stash "
                "was kept so nothing is lost",
                output=pop_out + pop_err,
            )
    return out.strip(), stashed

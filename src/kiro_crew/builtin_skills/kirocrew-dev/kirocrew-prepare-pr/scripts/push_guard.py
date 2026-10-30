#!/usr/bin/env python3
"""push_guard.py - stale-base guard and commit builder for the kirocrew-prepare-pr skill.

The default mode verifies that HEAD is safe to force-push by checking:
0. Nothing is staged in this checkout that HEAD lacks (exit 41).
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. origin/<base> is an ancestor of HEAD (i.e. HEAD sits on the freshly
   fetched base tip, not on a stale fork point that would cause the squash
   to bake in reversions of newer base changes).
3. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits for a single-commit PR workflow; configurable
   via --max-ahead).
4. None of the ahead-commits are patch-equivalent to commits in a bounded
   window (REPLAY_HISTORY_WINDOW) of origin/<base> history.  Comparison uses
   `git patch-id --stable` so renames, whitespace, and commit metadata are
   ignored — only the semantic diff matters.  The window is bounded because
   full history is unbounded cost, and replayed commits from a recent stale
   fork are by construction recent.
5. Every path HEAD changes against origin/<base> was committed on this branch
   by this script (the build record), is published by another author's
   commit on the pushed branch, or is one you vouch for by naming it after
   ``--`` (read-only).

This prevents the catastrophic failure mode where a worktree branched from a
local integration trunk (kiki-trunk) carries 100+ unshipped commits that get
force-pushed to the remote feature branch, clobbering upstream work.

The other modes make the flow's commits, so none of them is built from paths
someone else staged (references/rationale.md):
--commit -m MSG -- PATH...     commit exactly the named paths (-F FILE for the
                               message from a file).
--amend [-m MSG] -- [PATH...]  the same, amending HEAD.
--squash [MESSAGE_FILE] [-- PATH...]
                               checks 0-5 (PATH... vouches for paths the record
                               lacks), then one commit of HEAD's TREE on
                               origin/<base>, made with commit-tree and moved
                               onto the branch by one compare-and-swap.
--require-single-on-base [-- PATH...]
                               after the fetch: HEAD's only parent is
                               origin/<base>, and check 5 holds. Then
                               sweep_check.py checks the class sweep file
                               (warns first; refuses once it is switched on).
--check-index                  report what is staged here, and why.

A PATH is relative to this directory, or ``:(top,literal)PATH`` from the top
(the form refusals print); --paths-from-file FILE adds a refusal's
NUL-separated list.

Every revision is the fully qualified refs/remotes/origin/<base> the fetch
writes.  A git command that fails or outlives its bound refuses with a message
naming it — never SAFE, and never a guessed cause.

Portable: stdlib only (Python 3.9+); shells out to git via argument lists.

Exit codes: EXIT_CODES.
"""

from __future__ import annotations

import argparse
import collections
import importlib.machinery
import importlib.util
import os
import posixpath  # noqa: F401 - read by functions rebound from push_guard_commit.py
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import typing
from types import FunctionType
from typing import TYPE_CHECKING, Any

# Single source of truth for the default max-ahead threshold.  Shared by
# preflight.py (which imports this constant) so the two scripts cannot drift.
DEFAULT_MAX_AHEAD = 5

# Maximum number of base-history commits to scan for patch-id equivalence in
# the replay-detection check.  Bounded because full history is unbounded cost,
# and replayed commits from a recent stale fork are by construction recent
# (the operator branched from a stale tip that was N commits behind; the
# replayed patches are in that window).  500 is generous — a typical PR
# workflow replays at most a few dozen commits from a stale integration trunk.
REPLAY_HISTORY_WINDOW = 500

EXIT_SAFE = 0
EXIT_ENV = 2
EXIT_REFUSED = 40
EXIT_FOREIGN_INDEX = 41
EXIT_USAGE = 64
# The one statement of what each exit code means; the skill docs and the
# pipeline-conductor brief are pinned against it.
EXIT_CODES = (
    (EXIT_SAFE, "safe, or done"),
    (EXIT_ENV, "could not run: not a git repository, or no git"),
    (EXIT_REFUSED, "refused: the stderr names the cause and its fix"),
    (EXIT_FOREIGN_INDEX, "refused: something is staged here that HEAD lacks, or mid-operation"),
    (EXIT_USAGE, "usage error: fix the command line and retry"),
)

# Backstop for one git command (imported by preflight.py).  Every probe child
# runs with no prompt and no stdin, so reaching it means git is wedged.
GIT_TIMEOUT_S = 300
# The base fetch is stopped only once its output has stopped growing for this
# long (it runs with --progress).  A slow link that keeps receiving is never
# cut off, which a total bound would do on every retry: a killed fetch keeps
# none of what it received.
FETCH_STALL_S = 120
# The commit-msg hook and signing are user code that may prompt; they get
# longer, and the caller's terminal and environment.
HOOK_TIMEOUT_S = 1800
# How much of a failed command's output a refusal quotes: the END of it,
# where git and a hook put the reason.
_STDERR_CAP = 300
# How long a stopped child gets between the polite stop (git removes its lock
# files and stops what it started) and the forced one.
_KILL_GRACE_S = 5
# How many unmarked staged paths a refusal prints before pointing at the full
# list (paths marked (*) are always all printed).
_LISTED_PATHS = 20
# How much of a sequencer todo list is read for its first instruction.
_TODO_HEAD_CHARS = 64 * 1024
# `git hook run` (and its --ignore-missing) first shipped in this git.
_HOOK_RUN_GIT = (2, 36)

# Files inside git's own directory, so none is ever committed.
_MESSAGE_PREFIX = "prepare-pr-commit-msg-"
# The build record lives in the COMMON git dir, one file per commit the guard
# made (named by its sha, holding the paths it committed), so every worktree of
# the repository reads it, no two writers share a file and no entry evicts
# another.
_BUILT_DIR = "push_guard-built"
# Entries older than this are pruned when one is written.  Losing one only
# costs a refusal, never a pass.
_BUILT_MAX_AGE_S = 180 * 24 * 3600
# How many of a branch's past tips (its reflog) the record lookup reads.
_REFLOG_TIPS = 1000
# Reflog subjects of the entries that (re)point a branch NAME at a new start
# (``git branch``/``switch -c``/``checkout -b`` and their forced forms, and
# ``git reset`` of the checked-out branch): the lookup reads no tip from
# before one.
_RESET_ENTRIES = ("branch: Created from ", "branch: Reset to ", "reset: moving to ")
# A path argument in this form is top-relative: the form every refusal prints
# and every list file holds, so a listed path can be named back as printed.
_TOP_LITERAL = ":(top,literal)"
# One list file per refusal, so a remedy always reads the list it printed.
_STAGED_LIST_PREFIX = "push_guard-staged-"
_UNRECORDED_LIST_PREFIX = "push_guard-unrecorded-"
_STAGED_LIST_MAX_AGE_S = 24 * 3600

# Variables that relocate the repository, index or history git reads.  A hook
# or ``git rebase --exec`` exports some of them; the guard always means the
# repository of its working directory.  Caller configuration
# (GIT_CONFIG_COUNT/PARAMETERS) is NOT relocation and passes through: it can
# carry the identity, hooks path or signing setup the caller chose.
_RELOCATION_VARS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
# Set on every child: history as committed.  No replace ref, and no legacy
# info/grafts file, can re-parent a commit.  The graft file named instead is a
# path that cannot exist (a name under a device file): git then reads no graft
# file and says nothing, where any file it can open, even an empty one, makes
# it print its "info/grafts is deprecated" advice from every child.
_SAFETY_ENV = {
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_GRAFT_FILE": os.path.join(os.devnull, "push_guard-no-grafts"),
}
# Set on every probe child (shared with preflight.py): no terminal prompt, one
# locale.  GIT_ASKPASS is left alone: it is also how a non-interactive
# credential source is wired.  Children that run user hooks or signing keep
# the caller's environment.
NO_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS_REQUIRE": "never",
    "LC_ALL": "C",
}

# Pseudo-refs ``git commit`` consumes, and the operation each belongs to.
_CONSUMED_HEADS = (
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)

# Test-only injection point: when set to a non-None list, run() uses it
# instead of resolving "git" from PATH.  Allows tests to monkeypatch a
# Python-based fake git directly (e.g. push_guard._GIT_CMD = [sys.executable,
# str(fake_script)]) without platform-specific PATH/shell wrappers or
# environment-variable indirection.
_GIT_CMD: list[str] | None = None

ChildResult = collections.namedtuple("ChildResult", "rc out err timed_out")


class Refused(Exception):
    """A refusal already reported to stderr; main() maps it to EXIT_REFUSED."""


class UsageError(Exception):
    """A command line the guard cannot act on; main() maps it to EXIT_USAGE."""


class _Terminated(BaseException):
    """SIGTERM or SIGHUP, raised so the same cleanup as for Ctrl-C runs."""


def child_env(probe=True, extra=None):
    """The environment for a git child (shared with preflight.py).

    os.environ without _RELOCATION_VARS, plus _SAFETY_ENV, plus NO_PROMPT_ENV
    for a ``probe`` child (anything that does not run user hooks or signing).
    """
    env = {k: v for k, v in os.environ.items() if k not in _RELOCATION_VARS}
    env.update(_SAFETY_ENV)
    if probe:
        env.update(NO_PROMPT_ENV)
    env.update(extra or {})
    return env


# What cmd.exe expands inside an argument when it runs a .cmd/.bat file
# (preflight.run refuses the same set).
_CMD_META = '^&|<>%!"\r\n'


def _which(name):
    """An executable from PATH's absolute entries only, or None.

    A bare name is searched in the current directory first on Windows (and a
    relative PATH entry resolves against it everywhere), and that directory is
    the checkout under guard.  Naming each directory makes shutil.which look
    there alone, with PATHEXT still applied.  Shared with sweep_check.py, which
    runs git and gh the same way.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.isabs(entry):
            found = shutil.which(os.path.join(entry, name))
            if found:
                return found
    return None


def _which_git():
    """git from PATH's absolute entries only."""
    return _which("git")


def _git_argv(args):
    if args and args[0] == "git":
        if _GIT_CMD:
            return _GIT_CMD + list(args[1:])
        resolved = _which_git()
        # A bare name would let the OS search the checkout (CreateProcess
        # always, exec through a relative PATH entry): with no git on PATH's
        # absolute entries, fail as a missing executable instead.
        missing = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "git-not-on-PATH")
        return [resolved or missing] + list(args[1:])
    return list(args)


def tool_argv(args):
    """argv for git or gh, each resolved from PATH's absolute entries only (shared with sweep_check.py).

    With no such executable the argv names a missing file, so the launch fails
    as a missing executable instead of letting the OS search the checkout.
    """
    if args and args[0] == "gh":
        missing = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "gh-not-on-PATH")
        return [_which("gh") or missing] + list(args[1:])
    if args and args[0] == "git":
        return _git_argv(args)
    raise ValueError("only git and gh are run, not {!r}".format(args[0] if args else ""))


def _taskkill():
    """taskkill.exe by absolute path: a bare name is searched in the current
    directory first on Windows, and that directory is the checkout under guard."""
    for root in (os.environ.get("SystemRoot", ""), "C:\\Windows", "C:\\WINNT"):
        for sub in ("System32", "Sysnative"):
            candidate = "{}\\{}\\taskkill.exe".format(root.rstrip("\\/"), sub)
            if re.match(r"^[A-Za-z]:\\", candidate) and os.path.isfile(candidate):
                return candidate
    return None


def _stop(proc):
    """Stop a child and what it started; return once it is reaped.

    Children share the guard's process group, so whoever stops the guard's
    group (a harness timeout, a closed terminal, SIGKILL) stops them too.
    POSIX: SIGTERM first — git then removes its lock files and stops the
    transport it started — and the forced kill only after a grace period.
    Windows: the same two steps through taskkill /T, which follows the tree.
    """
    if sys.platform == "win32":
        taskkill = _taskkill()
        for force in ([], ["/F"]):
            if taskkill is not None:
                try:
                    subprocess.run(
                        [taskkill, "/T", *force, "/PID", str(proc.pid)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=_KILL_GRACE_S,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                proc.wait(timeout=_KILL_GRACE_S)
                return
            except subprocess.TimeoutExpired:
                pass
    else:
        try:
            proc.terminate()
            proc.wait(timeout=_KILL_GRACE_S)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def _wait(proc, timeout, stall, out, errf):
    """Wait for ``proc``; return True when it was stopped at a bound.

    ``timeout`` bounds the total; ``stall`` bounds how long its output may
    stop growing.  Either may be None.
    """
    if stall is None:
        try:
            proc.wait(timeout=timeout)
            return False
        except subprocess.TimeoutExpired:
            return True
    start = last_change = time.monotonic()
    last_size = -1
    while True:
        try:
            proc.wait(timeout=min(1.0, stall))
            return False
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        size = os.fstat(out.fileno()).st_size + os.fstat(errf.fileno()).st_size
        if size != last_size:
            last_size, last_change = size, now
        if now - last_change >= stall or (timeout is not None and now - start >= timeout):
            return True


def run_child(argv, env, timeout=None, input=None, stall=None, interactive=False, cwd=None):
    """Spawn ``argv`` and wait for it; return a ChildResult (shared by both scripts).

    stdout and stderr are temporary FILES, not pipes: a hook can background a
    child that inherits them, and waiting for a pipe's end would wait for
    that child, not for git.  An ``interactive`` child (hooks, signing) gets
    the caller's stdin and stderr instead, so a prompt reaches the user and
    git's own messages are shown as they happen; its ``err`` is empty.  A
    child stopped at a bound has ``timed_out`` set; on any interrupt it is
    stopped and reaped before re-raising.  A missing executable reports 127.

    A ``.cmd``/``.bat`` launch makes cmd.exe the parser of every argument, so
    one carrying a cmd.exe metacharacter is refused (126), never run.
    """
    if str(argv[0]).lower().endswith((".cmd", ".bat")) and any(
        ch in str(a) for a in argv[1:] for ch in _CMD_META
    ):
        return ChildResult(
            126,
            b"",
            "{}: refusing batch-file launch with cmd.exe metacharacters in arguments".format(
                argv[0]
            ).encode(),
            False,
        )
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errf:
        stdin: "int | typing.IO[bytes] | None" = None if interactive else subprocess.DEVNULL
        source = None
        if input is not None:
            source = tempfile.TemporaryFile()
            source.write(input.encode("utf-8") if isinstance(input, str) else input)
            source.seek(0)
            stdin = source
        try:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=stdin,
                    stdout=out,
                    stderr=None if interactive else errf,
                    env=env,
                    cwd=cwd,
                )
            except OSError as exc:
                return ChildResult(127, b"", "{}: {}".format(argv[0], exc).encode(), False)
            try:
                timed_out = _wait(proc, timeout, stall, out, errf)
            except BaseException:
                _stop(proc)
                raise
            if timed_out:
                _stop(proc)
        finally:
            if source is not None:
                source.close()
        out.seek(0)
        errf.seek(0)
        return ChildResult(proc.returncode, out.read(), errf.read(), timed_out)


def run(args, input=None, env=None, raw=False, timeout=None, stall=None):
    """Run a probe command; return (returncode, stdout, stderr).

    returncode is None when the command was stopped at its bound.  stdout is
    stripped text, or the raw bytes when ``raw`` (for ``-z`` output, where
    stripping would eat a path).  stderr is stripped text.

    Every probe git command goes through this runner -- including the
    ``patch-id`` calls, which hand their diff over ``input`` -- so the
    injection point below covers them.  The children that are not probes
    (``git commit``, ``commit-tree`` and the hooks, which may prompt) and ref
    moves go through run_child directly; all resolve git via _git_argv.

    When the module-level _GIT_CMD is set (test monkeypatch), "git" is
    replaced with the specified command list — no PATH/shell wrappers or
    environment-variable indirection needed.  Otherwise git is resolved via
    shutil.which so PATH-injected wrappers (including .bat/.cmd on Windows)
    are found without shell=True.
    """
    bound = GIT_TIMEOUT_S if timeout is None and stall is None else timeout
    res = run_child(_git_argv(args), child_env(extra=env), bound, input=input, stall=stall)
    err_text = res.err.decode("utf-8", "replace").strip()
    rc = None if res.timed_out else res.rc
    if res.timed_out:
        err_text = (
            "made no progress for {} s".format(stall)
            if stall
            else "timed out after {} s".format(bound)
        )
    if raw:
        return rc, res.out, err_text
    return rc, res.out.decode("utf-8", "replace").strip(), err_text


def err(msg):
    sys.stderr.write(msg + "\n")


def _tail(text):
    """The end of a command's output, where the reason is."""
    text = text.strip()
    return text if len(text) <= _STDERR_CAP else "…" + text[-_STDERR_CAP:]


def _failed(args, rc, stderr):
    """Report a failed or timed-out command by name."""
    what = " ".join(args)
    if rc is None:
        err("REFUSED: {} timed out.".format(what))
    else:
        err("REFUSED: {} failed (exit {}): {}".format(what, rc, _tail(stderr)))


def _ask(args, answers=(0,), env=None, raw=False, input=None):
    """Run a git command whose exit codes in ``answers`` are its answer; return (rc, stdout).

    Any other exit — a failure or a timeout — is reported, naming the
    command, and raises Refused, so no caller can read it as a verdict.
    """
    rc, out, stderr = run(args, env=env, raw=raw, input=input)
    if rc in answers:
        return rc, out
    _failed(args, rc, stderr)
    raise Refused()


def _remote_ref(base):
    """The ref the fetch writes.  The short ``origin/<base>`` resolves a stray
    local branch or tag of that name first (git warns, but the guard never
    sees the warning)."""
    return "refs/remotes/origin/" + base


def parse_origin_head(full):
    """The base named by ``refs/remotes/origin/HEAD``'s full target, or None (shared)."""
    prefix = _remote_ref("")
    if full.startswith(prefix) and len(full) > len(prefix):
        return full[len(prefix) :]
    return None


def _origin_head():
    """(the base origin/HEAD names, "main" when it is unset, else None; its full target)."""
    rc, full = _ask(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], answers=(0, 1))
    return ("main" if rc == 1 else parse_origin_head(full)), full


def _resolve_base(base_arg):
    """Resolve the base branch name from arg, origin/HEAD, or "main"."""
    if base_arg:
        return base_arg
    base, full = _origin_head()
    if base is None:
        err("REFUSED: refs/remotes/origin/HEAD points at {}; pass --base.".format(full))
        raise Refused()
    return base


def _local_base(base_arg):
    """(base, the sha refs/remotes/origin/<base> holds now, or None); fetches nothing.

    The base is resolved as _resolve_base does, but a name it cannot resolve,
    or a ref that does not exist yet, is ``("<base>", None)``, never a refusal.
    """
    base = base_arg or _origin_head()[0]
    if not base:
        return "<base>", None
    rc, sha = _ask(
        ["git", "rev-parse", "--verify", "--quiet", _remote_ref(base) + "^{commit}"],
        answers=(0, 1),
    )
    return base, sha if rc == 0 and sha else None


def _ssh_with_batch_mode(command):
    """``command`` with its non-interactive switch, or unchanged if unknown."""
    try:
        program = os.path.basename(shlex.split(command)[0]).lower()
    except (ValueError, IndexError):
        return command
    if program in ("ssh", "ssh.exe"):
        return command + " -oBatchMode=yes"
    if program in ("plink", "plink.exe", "tortoiseplink", "tortoiseplink.exe"):
        return command + " -batch"
    return command


def ssh_batch_env(run_fn):
    """The GIT_SSH_COMMAND that keeps the transport from prompting (shared with preflight).

    Starts from what the transport would run (inherited ``GIT_SSH_COMMAND``,
    else ``core.sshCommand`` read through ``run_fn``) so a per-repo identity
    survives.  A legacy ``GIT_SSH`` program takes no options and is left alone.
    """
    command = os.environ.get("GIT_SSH_COMMAND") or run_fn(["git", "config", "core.sshCommand"])[1]
    if command:
        return {"GIT_SSH_COMMAND": _ssh_with_batch_mode(command)}
    if os.environ.get("GIT_SSH"):
        return {}
    return {"GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


# Allowlist of known git fetch failure classes.  Each entry is a tuple of
# (compiled regex applied case-insensitively to stderr, user-facing label).
# When none match, the diagnostic withholds the raw text entirely — free-text
# stderr cannot be closed by shape enumeration alone (round-13 lesson).
_FETCH_ERROR_CLASSES: list[tuple["re.Pattern[str]", str]] = [
    (
        re.compile(r"interactivity has been disabled|terminal prompts disabled", re.IGNORECASE),
        "credentials need a prompt — run git fetch once interactively (or load "
        "your key into ssh-agent)",
    ),
    (
        re.compile(r"permission denied \(publickey", re.IGNORECASE),
        "SSH key not loaded or not accepted — run ssh-add, then retry",
    ),
    (re.compile(r"could not resolve host", re.IGNORECASE), "could not resolve host"),
    (re.compile(r"name or service not known", re.IGNORECASE), "DNS resolution failed"),
    (re.compile(r"permission denied", re.IGNORECASE), "permission denied"),
    (re.compile(r"repository not found", re.IGNORECASE), "repository not found"),
    (re.compile(r"does not appear to be a git repo", re.IGNORECASE), "not a git repository"),
    (re.compile(r"not a git repository", re.IGNORECASE), "not a git repository"),
    (re.compile(r"timed? ?out", re.IGNORECASE), "connection timed out"),
    (re.compile(r"connection refused", re.IGNORECASE), "connection refused"),
    (re.compile(r"connection reset", re.IGNORECASE), "connection reset"),
    (re.compile(r"couldn't connect to server", re.IGNORECASE), "could not connect"),
    (re.compile(r"ssl|tls|certificate", re.IGNORECASE), "TLS/certificate error"),
    (re.compile(r"authentication failed", re.IGNORECASE), "authentication failed"),
    (re.compile(r"invalid credentials", re.IGNORECASE), "authentication failed"),
    (re.compile(r"remote:.*not found", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"couldn't find remote ref", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"no matching remote head", re.IGNORECASE), "remote ref not found"),
]


def _classify_fetch_error(stderr: str) -> str:
    """Derive a safe diagnostic from git fetch stderr.

    Returns one of:
    - A matched error class label (e.g. "could not resolve host") when stderr
      contains a recognized git/ssh/curl failure pattern.
    - A generic withholding message when no pattern matches — free-text stderr
      can carry bare tokens (e.g. from ext:: remote helpers, pre-push hook
      output, or credential-helper error messages) that no URL-shape scrubber
      can redact.  The operator can run ``git fetch`` manually to see the full
      error in their own terminal.

    Contract: the return value NEVER contains raw stderr content that did not
    match the allowlist.  Only the matched class label (a hardcoded literal)
    is surfaced.  This closes the free-text credential egress class entirely
    rather than chasing individual shapes (round-13 lesson: 13 consecutive
    rounds of shape enumeration proved the approach cannot converge).
    """
    for pattern, label in _FETCH_ERROR_CLASSES:
        if pattern.search(stderr):
            return label
    return "fetch failed (details withheld — run git fetch manually to see the error)"


def fetch_diagnostic(rc, stderr):
    """The safe one-line cause of a failed base fetch (shared with preflight).

    ``rc`` None (this module's run) or 124 (preflight's run) is a fetch that
    was stopped because it made no progress.
    """
    if rc is None or rc == 124:
        return (
            "git fetch made no progress for {} s and was stopped; run git fetch "
            "origin <base> by hand once (it is not bounded there), then retry".format(FETCH_STALL_S)
        )
    return _classify_fetch_error(stderr)


def fetch_base_ref(base, run_fn=None):
    """Fetch origin/<base> into refs/remotes/origin/<base>; return (rc, stderr).

    Uses an explicit refspec (+refs/heads/<base>:refs/remotes/origin/<base>)
    so the remote-tracking ref is always updated regardless of the clone's
    configured remote.origin.fetch (e.g. single-branch clones, narrow CI
    checkouts).  The leading '+' ensures non-fast-forward updates are accepted
    (required after an upstream force-push of the base branch).  Submodules
    are not fetched: the base ref is all either script reads, and the batch
    ssh command would override each submodule's own transport.  ``--progress``
    is what the stall bound (FETCH_STALL_S) watches.
    """
    run_fn = run_fn or run
    refspec = "+refs/heads/{}:{}".format(base, _remote_ref(base))
    rc, _, stderr = run_fn(
        ["git", "fetch", "--progress", "--no-recurse-submodules", "origin", refspec],
        env=ssh_batch_env(run_fn),
        stall=FETCH_STALL_S,
    )
    return rc, stderr


def _fetch_base(base):
    """Fetch origin/<base>, refusing (Refused) on failure.

    The diagnostic is the classified error from ``fetch_diagnostic`` — never
    raw stderr, which may contain bare tokens from remote helpers or
    credential error messages.
    """
    print("Fetching origin/{} ...".format(base))
    rc, stderr = fetch_base_ref(base)
    if rc != 0:
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify merge-base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error class: {}".format(base, fetch_diagnostic(rc, stderr).replace("<base>", base))
        )
        raise Refused()


def _sha(rev):
    """Resolve ``rev`` to a commit sha."""
    return _ask(["git", "rev-parse", "--verify", "--quiet", rev + "~0"])[1]


def _ident(line):
    """The "Name <email>" of an ident line, without its timestamp."""
    return line.rsplit(" ", 2)[0]


def _commit_info(sha):
    """(tree, [parents], "Name <email>") read from the commit object."""
    body = _ask(["git", "cat-file", "commit", sha])[1]
    tree, parents, author = "", [], ""
    # split("\n"), never splitlines(): an ident may hold U+2028 or \x1c.
    for line in body.split("\n\n", 1)[0].split("\n"):
        key, _, value = line.partition(" ")
        if key == "tree":
            tree = value
        elif key == "parent":
            parents.append(value)
        elif key == "author":
            author = _ident(value)
    return tree, parents, author


def _write_git_file(path, data):
    """Write a file under the git dir; refuse (never a traceback) when it cannot."""
    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        err("REFUSED: cannot write {}: {}".format(path, exc))
        raise Refused()


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _empty_tree():
    return _ask(["git", "hash-object", "-t", "tree", "--stdin"], input=b"")[1]


def _changed(old, new):
    """Top-relative paths (bytes) that differ between two tree-ishes, renames as two."""
    out = _ask(
        [
            "git",
            "diff-tree",
            "-r",
            "-z",
            "--name-only",
            "--no-renames",
            "--ignore-submodules=none",
            old,
            new,
            "--",
        ],
        raw=True,
    )[1]
    return {p for p in out.split(b"\0") if p}


def _own_diff(sha):
    """Paths ``sha`` changes against its first parent (or the empty tree)."""
    parents = _commit_info(sha)[1]
    return _changed(parents[0] if parents else _empty_tree(), sha)


def _head_ref():
    """The branch ref HEAD points at, or "HEAD" when it is detached."""
    rc, ref = _ask(["git", "symbolic-ref", "--quiet", "HEAD"], answers=(0, 1))
    return ref if rc == 0 and ref else "HEAD"


def _diff_against(base):
    return "git diff {}..HEAD".format(_remote_ref(base))


def _refuse_unrecorded(paths, what, git_dir, base, mode, max_ahead=DEFAULT_MAX_AHEAD):
    """Report paths the guard did not commit on this branch; raise Refused.

    ``mode`` says what naming the paths back does: "check" (the default mode)
    and "single" (--require-single-on-base) only read them, "squash" commits
    them into the squash, "amend" commits their worktree copies.
    """
    shown = _indented(paths[:_LISTED_PATHS])
    if len(paths) > _LISTED_PATHS:
        shown += "\n    ... and {} more".format(len(paths) - _LISTED_PATHS)
    list_file = _write_list(git_dir, paths, _UNRECORDED_LIST_PREFIX)
    named = "named after -- (as listed, or --paths-from-file <list>)"
    if mode == "amend":
        read = "git show HEAD -- <path>, and git diff HEAD -- <path> for what is not committed yet"
        yours = (
            "re-run this same --amend with them {}. Naming a path commits its "
            "worktree copy into the amended commit, uncommitted edits included.".format(named)
        )
    elif mode == "squash":
        read = _diff_against(base) + " -- <path>"
        yours = (
            "re-run this same --squash command with them {}. The squash then "
            "commits them, and records them as yours.".format(named)
        )
    else:
        read = _diff_against(base) + " -- <path>"
        yours = "vouch for them by re-running this same command with them {}; it only reads them.".format(
            named
        )
        if mode == "check":
            yours += (
                " To rebuild the branch as one commit instead: push_guard.py --base {} "
                "--max-ahead {} --squash <message-file> -- <path>...".format(base, max_ahead)
            )
    err(
        "REFUSED: {} changes {} path(s) push_guard.py did not commit on this "
        "branch, so nothing shows they hold only your changes:\n{}\n"
        "  Full list (NUL-separated, as --paths-from-file reads it): {}\n"
        "  Read each one: {}.\n"
        "  All yours (for example committed by hand): {}\n"
        "  From another author's commit already on your pushed branch (a "
        "maintainer's or a co-author's): keep it. A path counts as published while "
        "HEAD holds exactly the copy origin/<branch> has; one a rebase changed needs "
        "reading, then naming as above.\n"
        "  Any other path not yours: do NOT push, and never name it. Rebuild the "
        "branch from your own commits (git reflog) in your own worktree.".format(
            what, len(paths), shown, list_file or "(the git dir is not writable)", read, yours
        )
    )
    raise Refused()


def _display(raw):
    """One ``-z`` path for a human: shell-quoted when plain, escaped otherwise.

    The plain form is ``:(top,literal)<path>``, which git and this script both
    read as that top-relative path from any directory; an escaped one is
    named through its refusal's list file (``--paths-from-file``).
    """
    text = raw.decode("utf-8", "surrogateescape")
    spec = _TOP_LITERAL + text
    if spec.isascii() and spec.isprintable():
        return shlex.quote(spec)
    return "{} (escaped; use the list file)".format(ascii(text))


def _indented(paths):
    """One indented, displayable path per line."""
    return "\n".join("    " + _display(p) for p in paths)


def _git_paths(*names):
    """``git rev-parse --git-path`` for each name, decoded losslessly."""
    args = ["git", "rev-parse"]
    for name in names:
        args += ["--git-path", name]
    out = _ask(args, raw=True)[1]
    return [os.fsdecode(p) for p in out.split(b"\n")[: len(names)]]


def _follow_renames(own, base, other):
    """``own`` plus where HEAD's side renamed any of them since ``base``.

    A merge or a replayed commit that changes ``old`` lands on ``new`` when
    the other side renamed ``old`` to ``new``, so ``new`` is its own path too.
    """
    out = _ask(
        ["git", "diff-tree", "-r", "-z", "-M", "--name-status", base, other, "--"], raw=True
    )[1]
    fields = [f for f in out.split(b"\0") if f]
    i = 0
    followed = set(own)
    while i < len(fields):
        status = fields[i]
        if status[:1] in (b"R", b"C") and i + 2 < len(fields):
            old, new = fields[i + 1], fields[i + 2]
            if old in own:
                followed.add(new)
            i += 3
        else:
            i += 2
    return followed


def _top():
    return os.fsdecode(_ask(["git", "rev-parse", "--show-toplevel"], raw=True)[1].rstrip(b"\n"))


def _write_list(git_dir, paths, prefix=_STAGED_LIST_PREFIX):
    """A new list file of ``paths`` for this refusal (older ones pruned); "" when unwritable.

    NUL-separated ``:(top,literal)`` pathspecs: what git's
    ``--pathspec-from-file --pathspec-file-nul`` and this script's
    ``--paths-from-file`` both read.
    """
    try:
        for name in os.listdir(git_dir):
            path = os.path.join(git_dir, name)
            if name.startswith((_STAGED_LIST_PREFIX, _UNRECORDED_LIST_PREFIX)) and (
                time.time() - os.path.getmtime(path) > _STAGED_LIST_MAX_AGE_S
            ):
                _unlink(path)
    except OSError:
        pass
    list_file = os.path.join(git_dir, "{}{}-{}.nul".format(prefix, os.getpid(), time.time_ns()))
    try:
        with open(list_file, "wb") as handle:
            handle.write(b"".join(os.fsencode(_TOP_LITERAL) + p + b"\0" for p in paths))
    except OSError:
        return ""
    return list_file


def _move_ref(ref, new, old, reason):
    """Compare-and-swap ``ref`` from ``old`` to ``new``; return where it now is.

    The outcome is read back from the ref, never taken from the exit code: a
    reference-transaction hook can be slow or report a failure after the
    move landed.
    """
    run_child(
        _git_argv(["git", "update-ref", "-m", reason, ref, new, old]),
        child_env(),
        GIT_TIMEOUT_S,
    )
    return _sha(ref)


def _interactive(argv, extra=None):
    """Run a child that may run user code or prompt: the caller's env, stdin and stderr.

    Returns a ChildResult whose ``out`` is its stdout; git's messages and the
    hooks' were already shown on the caller's stderr.  It is in the guard's
    process group, so a Ctrl-C reaches it too; the guard still waits for it.
    """
    return run_child(
        _git_argv(argv), child_env(probe=False, extra=extra), HOOK_TIMEOUT_S, interactive=True
    )


def _outcome(res):
    """``exit N`` or ``timed out``, for a refusal."""
    return "timed out" if res.timed_out else "exit {}".format(res.rc)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "{}: error: {}\n".format(self.prog, message))


def _parse_args(argv=None):
    parser = _Parser(
        description="Pre-push stale-base guard and commit builder",
        epilog="exit codes: "
        + "; ".join("{} {}".format(code, meaning) for code, meaning in EXIT_CODES),
    )
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=DEFAULT_MAX_AHEAD,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: {}).".format(
            DEFAULT_MAX_AHEAD
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--require-single-on-base",
        action="store_true",
        help="Post-squash mode: HEAD's only parent is the freshly fetched "
        "origin/<base>, and this script committed every path HEAD changes.",
    )
    mode.add_argument("--check-index", action="store_true", help="Report what is staged here.")
    mode.add_argument(
        "--squash",
        nargs="?",
        const="",
        default=None,
        metavar="MESSAGE_FILE",
        help="Run the pre-squash checks, then squash the branch to one commit of "
        "HEAD's tree. The message defaults to <git-dir>/{}<branch>.txt. Paths after "
        "-- vouch for changes this script did not commit.".format(_MESSAGE_PREFIX),
    )
    mode.add_argument("--commit", action="store_true", help="Commit the named paths.")
    mode.add_argument("--amend", action="store_true", help="Amend HEAD with the named paths.")
    message = parser.add_mutually_exclusive_group()
    message.add_argument("-m", "--message", default=None, help="Message for --commit/--amend.")
    message.add_argument(
        "-F",
        "--message-file",
        default=None,
        metavar="FILE",
        help="Read the --commit/--amend message from FILE (keeps its text off the command line).",
    )
    parser.add_argument(
        "--paths-from-file",
        default=None,
        metavar="FILE",
        help="Also read paths from FILE, NUL-separated (a refusal's list file).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="After --: the paths to commit (--commit/--amend), or, in every other "
        "mode but --check-index, paths you vouch for that this script did not commit.",
    )
    args = parser.parse_args(argv)
    if args.paths_from_file is not None:
        try:
            with open(args.paths_from_file, "rb") as handle:
                listed = handle.read()
        except OSError as exc:
            parser.error("cannot read --paths-from-file {}: {}".format(args.paths_from_file, exc))
        args.paths = list(args.paths) + [os.fsdecode(p) for p in listed.split(b"\0") if p]
    has_message = args.message is not None or args.message_file is not None
    if args.message is not None and not args.message.strip():
        parser.error("-m needs a non-empty message")
    if args.message_file is not None and not args.message_file.strip():
        parser.error("-F needs a file name")
    if any(not path.strip() for path in args.paths):
        parser.error("an empty path names nothing (an unset shell variable?); name each path")
    if args.commit and not (has_message and args.paths):
        parser.error("--commit needs -m MESSAGE (or -F FILE) and the paths to commit, after --")
    if has_message and not (args.commit or args.amend):
        parser.error("-m and -F are only for --commit/--amend")
    if args.paths and args.check_index:
        parser.error("--check-index takes no paths")
    if args.squash == "-" or args.message_file == "-":
        parser.error(
            "{} reads a message FILE; stdin (-) is not accepted".format(
                "--squash" if args.squash == "-" else "-F"
            )
        )
    return args


def _main(args):
    # Must be in a git repo.  Only git's own answers mean "cannot run".
    probe = ["git", "rev-parse", "--is-inside-work-tree", "--absolute-git-dir"]
    rc, out, probe_err = run(probe, raw=True)
    if rc == 127:
        err("ERROR: git is not available: {}".format(probe_err))
        return EXIT_ENV
    if rc != 0:
        if "not a git repository" in probe_err.lower():
            err("ERROR: not inside a git repository.")
            return EXIT_ENV
        _failed(probe, rc, probe_err)
        return EXIT_REFUSED
    git_dir = os.fsdecode(out.rstrip(b"\n").split(b"\n")[-1])

    if args.check_index:
        return _check_index(git_dir, report=True)
    if args.commit or args.amend:
        # The -m text as bytes, exactly as the shell passed them (any encoding).
        message = None if args.message is None else os.fsencode(args.message)
        rc = _commit_by_name(args.paths, message, args.message_file, args.amend, git_dir, args.base)
        if rc == 0:
            print("STATUS: COMMITTED (the named paths only)")
        return rc

    # Cheap local preconditions before the fetch and the history scan.  The
    # index check comes first: mid-operation, HEAD is detached, and that is
    # the operation's state, not a branch to check out.
    rc = _check_index(git_dir)
    if rc:
        return rc
    ref = message = message_path = None
    vouched = _named_paths(args.paths, os.path.realpath(_top())) if args.paths else {}
    if args.squash is not None:
        ref = _branch_ref()
        message_path = (
            os.path.abspath(args.squash)
            if args.squash
            else squash_message_path(git_dir, ref[len("refs/heads/") :])
        )
        message = _read_message(message_path)
    base = _resolve_base(args.base)
    if ref == "refs/heads/" + base:
        err(
            "REFUSED: the checked-out branch is the base ({}); squashing would "
            "rewrite it. Check out your feature branch.".format(base)
        )
        return EXIT_REFUSED
    _fetch_base(base)

    if args.require_single_on_base:
        _check_single_on_base(base, git_dir, vouched)
        if _sweep_check(base, git_dir):
            return EXIT_REFUSED
        print("STATUS: SAFE TO PUSH (single commit on base)")
        return 0
    head, pinned, ahead = _check_pre_squash(base, args.max_ahead)
    if args.squash is None:
        unknown = _unrecorded(
            git_dir, _head_ref(), _changed(pinned, head), ahead, pinned, head, vouched
        )
        if unknown:
            _refuse_unrecorded(unknown, "HEAD", git_dir, base, "check", args.max_ahead)
        print("STATUS: SAFE TO PUSH")
        return 0
    _squash(base, ref, head, pinned, ahead, git_dir, message, vouched)
    if not args.squash and message_path:
        # Only the default file is the guard's to consume; a named one is the caller's.
        _unlink(message_path)
    print("STATUS: SQUASHED (HEAD's tree, one commit on origin/{})".format(base))
    return 0


def _raise_terminated(signum, frame):
    raise _Terminated()


def install_termination_handlers():
    """Make SIGTERM and SIGHUP raise _Terminated (shared with preflight.py).

    A signal the caller already ignores (``nohup``) stays ignored, and so it
    stays ignored in the git children too.  The raise unwinds through
    run_child, which stops and reaps the child it was waiting for.  Returns
    what to hand to restore_handlers().
    """
    previous = []
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        # An inherited ignore (nohup) is kept, as Python keeps it for SIGINT.
        if number is None or signal.getsignal(number) == signal.SIG_IGN:
            continue
        try:
            previous.append((number, signal.signal(number, _raise_terminated)))
        except (OSError, ValueError):  # not the main thread
            pass
    return previous


def restore_handlers(previous):
    for number, handler in previous:
        signal.signal(number, handler)


_INTERRUPTED = {
    "squash": "The branch moves only in one final step, so it holds either its old "
    "tip or the finished squash; nothing else changed.",
    "commit": "The commit is made in one step: HEAD is either the old commit or the "
    "new one (git log -1 shows which), and the index you share changes only "
    "after the commit lands.",
    "other": "This mode changes nothing.",
}


def main(argv=None):
    args = _parse_args(argv)
    if args.squash is not None:
        kind = "squash"
    elif args.commit or args.amend:
        kind = "commit"
    else:
        kind = "other"
    previous = install_termination_handlers()
    try:
        return _main(args)
    except Refused:
        return EXIT_REFUSED
    except UsageError:
        return EXIT_USAGE
    except (KeyboardInterrupt, _Terminated):
        err("REFUSED: interrupted. " + _INTERRUPTED[kind])
        return EXIT_REFUSED
    except OSError as exc:
        err("REFUSED: {}".format(exc))
        return EXIT_REFUSED
    finally:
        restore_handlers(previous)


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        return self.source_to_code(source, path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


def _load_sibling(filename, name):
    """Load a sibling script without cwd, sys.path, or bytecode side effects."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    loader = _NoBytecodeSourceLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot import kirocrew-prepare-pr sibling script: " + path)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _load_owner(stem):
    """Load the owner module ``<stem>.py`` beside this script.

    It is named under this module's own package, so a harness that loads the script
    under a dotted name gets its owners under the same prefix -- the name is what a
    coverage run measures by -- and a script run names them by their bare stem.
    """
    package = __name__.rpartition(".")[0]
    return _load_sibling(stem + ".py", package + "." + stem if package else stem)


def _sweep_check(base, git_dir):
    """Run sweep_check.py on the class-sweep file; 0 or EXIT_REFUSED.

    The check is advisory in its warn period and refuses once it is switched on.
    A check that cannot run says so and lets the push through: it never asks a person.
    """
    try:
        module = _load_sibling("sweep_check.py", "_prepare_pr_sweep_check")
        return EXIT_REFUSED if module.run(base, git_dir) == module.EXIT_REFUSED else 0
    except (OSError, ImportError, SyntaxError, AttributeError, TypeError, ValueError) as exc:
        err("WARNING: the class-sweep check could not run ({}); not blocking the push.".format(exc))
        return 0


def rebind(fn: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    """A copy of *fn* whose globals are *namespace*; every other attribute kept.

    A local twin of ``kiro_crew._facade_compose.rebind``: this script runs under a bare
    interpreter that cannot import the package, and a parity test keeps the two bodies
    equal. The one difference reads ``__type_params__`` with a default, because this
    script still runs on interpreters older than 3.12, which have no such attribute.
    """
    new = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    new.__kwdefaults__ = fn.__kwdefaults__
    new.__annotations__ = fn.__annotations__
    new.__dict__.update(fn.__dict__)
    new.__doc__ = fn.__doc__
    new.__qualname__ = fn.__qualname__
    new.__module__ = namespace["__name__"]
    new.__type_params__ = getattr(fn, "__type_params__", ())
    return new


def _bind_owners(stems):
    """Load each owner and bind every function it defines here, rebound onto this module.

    An owner function then reads this module's globals, so a name a test replaces here
    reaches it as it reached the one-file script. A function is an owner's when its code
    was compiled from that owner's file. A name this module already binds is refused,
    never overwritten.
    """
    namespace = globals()
    owners = []
    for stem in stems:
        owner = _load_owner(stem)
        for name, value in list(vars(owner).items()):
            if isinstance(value, FunctionType) and value.__code__.co_filename == owner.__file__:
                if name in namespace:
                    raise ImportError(
                        "{}.py defines {}, which push_guard.py binds".format(stem, name)
                    )
                namespace[name] = rebind(value, namespace)
        owners.append(owner)
    return tuple(owners)


# The guard's checks and commit builders live in five owners, each holding functions
# only. Constants, the exceptions, the git runner, the base fetch, the refusal lists and
# the command line stay here. Every owner function is rebound onto this module's
# globals below, before anything calls one.
if TYPE_CHECKING:
    from push_guard_commit import _commit_by_name, _named_paths
    from push_guard_history import _check_pre_squash, _check_single_on_base
    from push_guard_index import _check_index
    from push_guard_record import _unrecorded
    from push_guard_squash import _branch_ref, _read_message, _squash, squash_message_path
_OWNERS = _bind_owners(
    (
        "push_guard_index",
        "push_guard_record",
        "push_guard_history",
        "push_guard_squash",
        "push_guard_commit",
    )
)


if __name__ == "__main__":
    sys.exit(main())

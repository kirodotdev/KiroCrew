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
                               origin/<base>, and check 5 holds.
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
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import typing

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


def squash_message_path(git_dir, branch):
    """Where --squash reads the message for ``branch`` (``/`` becomes ``-``)."""
    return os.path.join(git_dir, _MESSAGE_PREFIX + branch.replace("/", "-") + ".txt")


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


def _which_git():
    """git from PATH's absolute entries only.

    A bare name is searched in the current directory first on Windows (and a
    relative PATH entry resolves against it everywhere), and that directory is
    the checkout under guard.  Naming each directory makes shutil.which look
    there alone, with PATHEXT still applied.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.isabs(entry):
            found = shutil.which(os.path.join(entry, "git"))
            if found:
                return found
    return None


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


def run_child(argv, env, timeout=None, input=None, stall=None, interactive=False):
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
                    argv, stdin=stdin, stdout=out, stderr=None if interactive else errf, env=env
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


def _resquash_remedy(base):
    """The one way this skill rebuilds a squash; every refusal prints it."""
    return (
        "rebase onto the fresh base (git rebase {}), then squash with "
        "push_guard.py --base {} --squash".format(_remote_ref(base), base)
    )


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


def _common_dir(git_dir):
    """The repository's common git dir: shared by all its worktrees."""
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as handle:
            relative = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return git_dir
    return os.path.normpath(os.path.join(git_dir, relative)) if relative else git_dir


def _built_dir(git_dir):
    return os.path.join(_common_dir(git_dir), _BUILT_DIR)


def _is_sha(text):
    return re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) is not None


def record_built(git_dir, sha, paths):
    """Record ``paths`` (top-relative bytes) as what the guard committed in ``sha``.

    One file per commit, written whole and moved into place, so a concurrent
    writer can never drop another's entry.  Entries past _BUILT_MAX_AGE_S are
    pruned on the way.  Refuses (never a traceback) when it cannot write.
    """
    directory = _built_dir(git_dir)
    if not _is_sha(sha):
        err("REFUSED: {!r} is not a commit id the record can hold.".format(sha))
        raise Refused()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        err("REFUSED: cannot write {}: {}".format(directory, exc))
        raise Refused()
    try:
        for name in os.listdir(directory):
            entry = os.path.join(directory, name)
            age = time.time() - os.path.getmtime(entry)
            if age > (_STAGED_LIST_MAX_AGE_S if name.endswith(".tmp") else _BUILT_MAX_AGE_S):
                _unlink(entry)
    except OSError:
        pass
    path = os.path.join(directory, sha)
    tmp = "{}.{}-{}.tmp".format(path, os.getpid(), time.time_ns())
    _write_git_file(tmp, b"".join(p + b"\0" for p in sorted(set(paths) | _built(path))))
    try:
        os.replace(tmp, path)
    except OSError as exc:
        _unlink(tmp)
        err("REFUSED: cannot write {}: {}".format(path, exc))
        raise Refused()


def _built(path):
    """The paths one record file holds; empty when there is none."""
    try:
        with open(path, "rb") as handle:
            return {p for p in handle.read().split(b"\0") if p}
    except OSError:
        return set()


def _head_ref():
    """The branch ref HEAD points at, or "HEAD" when it is detached."""
    rc, ref = _ask(["git", "symbolic-ref", "--quiet", "HEAD"], answers=(0, 1))
    return ref if rc == 0 and ref else "HEAD"


def _branch_tips(ref):
    """The past tips of the branch ``ref`` (its reflog), newest first.

    The walk stops at the entry that last created or reset the name (``git
    switch -C``, ``checkout -B``, ``branch -f`` and ``git reset`` keep the
    reflog and write one), so a reused name inherits nothing from what it
    named before.  A detached HEAD has no branch reflog: no tips.
    """
    if not ref.startswith("refs/heads/"):
        return []
    out = _ask(
        [
            "git",
            "log",
            "--walk-reflogs",
            "--no-show-signature",
            "--format=%H%x00%gs",
            "--max-count={}".format(_REFLOG_TIPS),
            ref,
            "--",
        ]
    )[1]
    tips = []
    for line in out.split("\n"):
        sha, _, subject = line.partition("\0")
        if not _is_sha(sha):
            continue
        tips.append(sha)
        if subject.startswith(_RESET_ENTRIES):
            break
    return list(dict.fromkeys(tips))


def _local_email():
    """The email of the identity this checkout commits as, or None when git has none."""
    rc, out, _ = run(["git", "var", "GIT_AUTHOR_IDENT"])
    if rc != 0 or not out:
        return None
    return _ident(out).rpartition("<")[2].rstrip(">").strip().lower()


def _published(ref, pinned, head, tips):
    """Paths another author's commit on the pushed branch changes that ``head`` holds as pushed.

    Those came from a commit already on the pull request -- a maintainer's or
    a co-author's -- not from this checkout's index.  The pushed branch
    (origin/<branch>) counts only when it is in this branch's lineage
    (reachable from ``head`` or one of its ``tips``), so a stale remote ref of
    an earlier branch with the same name counts for nothing.  Only commits
    whose author is not this checkout's identity count, so a hand commit of
    a stale copy pushed once without the guard is never published.  Only the
    branch's own changes count (against its merge-base with ``pinned``), so a
    pushed copy of an older base can never make a stale path look published.
    """
    if not ref.startswith("refs/heads/"):
        return set()
    remote = _remote_ref(ref[len("refs/heads/") :])
    rc, sha = _ask(
        ["git", "rev-parse", "--verify", "--quiet", remote + "^{commit}"], answers=(0, 1)
    )
    if rc != 0 or not sha:
        return set()
    lineage = "".join("^{}\n".format(t) for t in [head, *tips]).encode("ascii")
    if _ask(["git", "rev-list", "--max-count=1", sha, "--stdin"], input=lineage)[1]:
        return set()
    rc, mbase = _ask(["git", "merge-base", sha, pinned], answers=(0, 1))
    mine = _local_email()
    if rc != 0 or not mbase or mine is None:
        return set()
    out = _ask(
        [
            "git",
            "rev-list",
            "--format=%ae",
            "--max-count={}".format(_REFLOG_TIPS + 1),
            "{}..{}".format(mbase, sha),
            "--",
        ]
    )[1]
    # rev-list --format prints a "commit <sha>" line, then the format.
    lines = out.split("\n")
    authored = list(zip(lines[0::2], lines[1::2]))
    if len(authored) > _REFLOG_TIPS:
        return set()
    theirs: set = set()
    for header, email in authored:
        if email.strip().lower() != mine:
            theirs |= _own_diff(header.split(" ", 1)[1])
    return theirs & (_changed(mbase, sha) - _changed(sha, head))


def _unrecorded(git_dir, ref, paths, commits, pinned=None, head=None, vouched=()):
    """The ``paths`` nothing accounts for, sorted.

    A path is accounted for when it is named in ``vouched``; when the guard
    recorded it for one of ``commits`` (the branch's own: what HEAD carries);
    when it recorded it for a past tip of the branch ``ref`` (_branch_tips:
    what a rebase or squash rewrote), followed through the renames the base
    made since, given ``pinned``; or, given ``head`` (a commit), when it is
    published on the pushed branch (_published).  A detached HEAD has no
    branch reflog, so only its own commits count; a deleted, re-created or
    reset branch name has no tips from before, so nothing recorded for what
    it named before carries over.
    """
    unknown = set(paths) - set(vouched)
    if not unknown:
        return []
    directory = _built_dir(git_dir)
    try:
        names = set(os.listdir(directory))
    except OSError:
        names = set()
    commits = set(commits)
    for sha in commits:
        if unknown and sha in names:
            unknown -= _built(os.path.join(directory, sha))
    tips = _branch_tips(ref) if unknown else []
    past = []
    for sha in tips:
        if unknown and sha in names and sha not in commits:
            own = _built(os.path.join(directory, sha))
            unknown -= own
            past.append((sha, own))
    if unknown and pinned and past:
        by_base: dict = {}
        for sha, own in past:
            rc, mbase = _ask(["git", "merge-base", sha, pinned], answers=(0, 1))
            if rc == 0 and mbase and mbase != pinned:
                by_base.setdefault(mbase, set()).update(own)
        for mbase, own in by_base.items():
            if unknown:
                unknown -= _follow_renames(own, mbase, pinned)
    if unknown and pinned and head:
        unknown -= _published(ref, pinned, head, tips)
    return sorted(unknown)


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


def _check_single_on_base(base, git_dir, vouched=()):
    """Post-squash guard: HEAD's ONLY parent is origin/<base>, and the record covers HEAD."""
    pinned = _sha(_remote_ref(base))
    head = _sha("HEAD")
    tree, parents, _ = _commit_info(head)
    shown = " ".join(p[:12] for p in parents) or "none"

    print("parents:         " + shown)
    print("origin/{}:     {}".format(base, pinned[:12]))
    print("HEAD:            " + head[:12])

    if parents != [pinned]:
        err(
            "REFUSED: HEAD's parents ({}) are not exactly origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: {}.".format(shown, base, pinned[:12], _resquash_remedy(base))
        )
        raise Refused()
    unknown = _unrecorded(
        git_dir, _head_ref(), _changed(pinned, tree), [head], pinned, head, vouched
    )
    if unknown:
        _refuse_unrecorded(unknown, "HEAD ({})".format(head[:12]), git_dir, base, "single")


def _patch_ids(revs):
    """patch-id -> sha for ``revs`` (fail closed: a git failure refuses)."""
    found: dict[str, str] = {}
    for commit_sha in revs:
        diff_out = _ask(["git", "diff-tree", "-p", commit_sha, "--"])[1]
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        pid_out = _ask(["git", "patch-id", "--stable"], input=diff_out)[1]
        if pid_out:
            found[pid_out.split()[0]] = commit_sha
    return found


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    HEAD and origin/<base> are resolved ONCE; every check reads those shas,
    which are returned as (head, pinned, the commits between them).  Fail-closed contract: every git
    failure, timeout or OSError refuses (Refused) with a diagnostic naming
    the failed command.  The ONLY path that returns is one where every git
    command SUCCEEDED.
    """
    pinned = _sha(_remote_ref(base))
    head = _sha("HEAD")
    rc, merge_base = _ask(["git", "merge-base", head, pinned], answers=(0, 1))
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        raise Refused()

    # origin/<base> must be an ancestor of HEAD — HEAD sits on the fetched
    # tip.  After a correct rebase (Phase 1 step 2) this always holds; if it
    # fails, a squash would bake in reversions of newer base changes.
    if merge_base != pinned:
        err(
            "REFUSED: HEAD is not based on the fresh origin/{} tip — the "
            "branch forks from a stale base and squashing would bake in "
            "reversions of newer base changes. To fix: {}.".format(base, _resquash_remedy(base))
        )
        raise Refused()

    ahead_revs = _ask(["git", "rev-list", "{}..{}".format(pinned, head), "--"])[1].split()
    ahead = len(ahead_revs)

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, pinned[:12]))
    print("HEAD:            " + head[:12])
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix (if you authored all {} commits): squash them down "
            "(push_guard.py --base {} --squash --max-ahead {}) so the branch "
            "carries a single deliverable commit.\n"
            "  To fix (if any ahead-commit is unfamiliar): STOP and diagnose — "
            "do not squash foreign history. The branch may have picked up "
            "commits from a local integration trunk.\n"
            "  To fix (stale fork): {}.".format(
                ahead, base, max_ahead, ahead, base, ahead, _resquash_remedy(base)
            )
        )
        raise Refused()

    # Detect replayed commits: an ahead-commit patch-equivalent to a commit in
    # a BOUNDED window (REPLAY_HISTORY_WINDOW) of base history replays
    # upstream patches (e.g. a stale fork that cherry-picked base commits).
    ahead_ids = _patch_ids(ahead_revs)
    if ahead_ids:
        base_out = _ask(
            ["git", "rev-list", "--max-count={}".format(REPLAY_HISTORY_WINDOW), pinned, "--"]
        )[1]
        base_ids = _patch_ids(base_out.split())
        replayed = [(a, base_ids[pid]) for pid, a in ahead_ids.items() if pid in base_ids]
        if replayed:
            err(
                "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
                "already on origin/{} — the branch replays upstream history.\n"
                "  replayed (ahead ↔ base): {}\n"
                "  To fix: STOP and diagnose. Do not squash — one or more "
                "ahead-commits duplicate patches already on the base branch. "
                "Rebase onto a fresh origin/{} so only novel changes remain, "
                "or cherry-pick your original commits onto origin/{}.".format(
                    len(replayed),
                    base,
                    ", ".join("{} ↔ {}".format(a[:12], b[:12]) for a, b in replayed),
                    base,
                    base,
                )
            )
            raise Refused()
    return head, pinned, ahead_revs


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


def _sequencer_op(sequencer):
    """The operation a sequencer-only stop belongs to, from its todo list."""
    try:
        with open(os.path.join(sequencer, "todo"), encoding="utf-8", errors="replace") as handle:
            # The first instruction is all it needs: read a bounded head only.
            head = handle.read(_TODO_HEAD_CHARS)
    except OSError:
        return "cherry-pick"
    for line in head.split("\n"):
        word = line.split(None, 1)[0] if line.strip() else ""
        if word and not word.startswith("#"):
            return "revert" if word in ("revert", "r") else "cherry-pick"
    return "cherry-pick"


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


def _in_progress():
    """(operation, its own paths or None) when one is stopped mid-way, else None.

    Decided the way ``git status`` decides it: a rebase is running only while
    its state directory exists, and only the pseudo-refs ``git commit``
    consumes count otherwise.  A stale REBASE_HEAD left by a finished rebase
    is not a rebase in progress.  The own paths are the stopped commit's,
    plus where the other side renamed them.
    """
    rebase_merge, rebase_apply, sequencer, applying = _git_paths(
        "rebase-merge", "rebase-apply", "sequencer", "rebase-apply/applying"
    )
    stopped = []
    for ref, op in _CONSUMED_HEADS:
        rc, sha = _ask(["git", "rev-parse", "-q", "--verify", ref], answers=(0, 1))
        if rc == 0:
            stopped.append((op, sha))
    if os.path.isdir(rebase_merge) or os.path.isdir(rebase_apply):
        op = "am" if os.path.exists(applying) else "rebase"
        rc, sha = _ask(["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"], answers=(0, 1))
        stopped.insert(0, (op, sha if rc == 0 else ""))
    elif os.path.isdir(sequencer) and not stopped:
        return _sequencer_op(sequencer), None
    if not stopped:
        return None
    op, sha = stopped[0]
    if not sha:
        return op, set()
    if op == "merge":
        rc, mbase = _ask(["git", "merge-base", "HEAD", sha], answers=(0, 1))
        old = mbase if rc == 0 else _empty_tree()
    else:
        parents = _commit_info(sha)[1]
        old = parents[0] if parents else _empty_tree()
    own = _changed(old, sha)
    return op, _follow_renames(own, sha if op == "revert" else old, "HEAD")


def _staged():
    """[(status letter, top-relative path bytes)] staged that HEAD lacks (read-only)."""
    status = _ask(
        [
            "git",
            "diff-index",
            "--cached",
            "--no-renames",
            "--ignore-submodules=none",
            "--ita-invisible-in-index",
            "--name-status",
            "-z",
            "HEAD",
            "--",
        ],
        raw=True,
    )[1]
    fields = [f for f in status.split(b"\0") if f]
    return list(zip(fields[0::2], fields[1::2]))


def _unmerged():
    """Top-relative paths (bytes) with unresolved conflicts, from any subdirectory."""
    out = _ask(["git", "ls-files", "-u", "-z", "--full-name", "--", ":(top)"], raw=True)[1]
    return sorted({entry.split(b"\t", 1)[1] for entry in out.split(b"\0") if b"\t" in entry})


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


def _carry_remedy():
    return (
        "git worktree add -b <new-branch> <dir> HEAD (it carries your commits, and "
        "the record of those the guard made); move "
        "uncommitted edits of yours with git diff --binary HEAD -- <your paths> > "
        "<patch-file> and git -C <dir> apply <patch-file> (never git stash)"
    )


def _report_staged(foreign, git_dir, lead="", op=None):
    """Print the refusal for staged paths HEAD lacks; return EXIT_FOREIGN_INDEX.

    Every path whose worktree copy may hold edits is listed and marked (*),
    however many there are; the command that clears the list is offered only
    when none is marked, and never in the middle of an operation.
    """
    edited = {
        p
        for p in _ask(
            ["git", "diff-files", "--name-only", "-z", "--ignore-submodules=none"], raw=True
        )[1].split(b"\0")
        if p
    }
    top = os.fsencode(_top())

    def marked(letter, path):
        # A staged deletion is absent from the index, so diff-files cannot see
        # it; a copy still on disk may hold edits.
        if letter == b"D":
            return os.path.lexists(os.path.join(top, path))
        return path in edited

    flagged = [(p, marked(s, p)) for s, p in foreign]
    n_marked = sum(1 for _, m in flagged if m)
    budget = max(0, _LISTED_PATHS - n_marked)
    lines = []
    hidden = 0
    for path, mark in flagged:
        if mark:
            lines.append("    {}   (*)".format(_display(path)))
        elif budget:
            budget -= 1
            lines.append("    " + _display(path))
        else:
            hidden += 1
    if hidden:
        lines.append("    ... and {} more, none marked (*)".format(hidden))
    list_file = _write_list(git_dir, [p for _, p in foreign])
    added = sum(1 for s, _ in foreign if s == b"A")
    text = [
        "REFUSED: {}{} staged path(s) differ from HEAD ({} not in HEAD):".format(
            lead, len(foreign), added
        ),
        "\n".join(lines),
        "  Full list (NUL-separated pathspecs): {}".format(
            list_file or "(the git dir is not writable)"
        ),
    ]
    if op is None:
        text.append(
            "  Yours: commit them by name — push_guard.py --commit -m '<subject>' -- <path>..."
        )
        text.append(
            "  Not yours (another session staged here, e.g. `git checkout <ref> -- .`): "
            "leave this checkout alone and continue in your own: " + _carry_remedy() + "."
        )
        if n_marked:
            text.append(
                "  No command here clears them: (*) marks a worktree copy that differs "
                "from what is staged, so it may hold edits of yours."
            )
        elif list_file:
            text.append(
                "  To clear them here instead — only when none is yours: git restore "
                "--source=HEAD --staged --worktree --pathspec-from-file={} "
                "--pathspec-file-nul".format(shlex.quote(list_file))
            )
    else:
        text.append(
            "  Yours (staged on purpose during the {0}): unstage them, keeping your "
            "copy (git restore --staged -- <path>...), finish the {0}, then commit them "
            "by name.".format(op)
        )
        text.append(
            "  Not yours: leave them, abort the {} (git {} --abort), and continue in "
            "your own worktree: {}.".format(op, op, _carry_remedy())
        )
        if n_marked:
            text.append(
                "  (*) the worktree copy differs from what is staged, so it may hold "
                "edits of yours."
            )
    err("\n".join(text))
    return EXIT_FOREIGN_INDEX


def _check_index(git_dir, report=False):
    """Refuse an index that holds anything HEAD lacks; return 0 or EXIT_FOREIGN_INDEX.

    Read-only: ``diff-index --cached`` takes no lock and writes nothing, and a
    name-only listing never reads blob content.  At a stopped rebase, merge,
    cherry-pick or revert, that operation's own paths are expected; anything
    else staged is listed.  With ``report`` (--check-index), a clean index
    prints its STATUS, and a stop whose staged paths are all its own is 0 —
    safe to ``--continue``.
    """
    progress = _in_progress()
    conflicts = _unmerged()
    if conflicts:
        shown = _indented(conflicts[:_LISTED_PATHS])
        if progress is None:
            err(
                "REFUSED: the index holds unresolved conflicts (e.g. from a git stash "
                "pop):\n{}\n  Resolve them and read the result with git diff before any "
                "commit; never commit a file that still carries conflict markers.".format(shown)
            )
        else:
            err(
                "REFUSED: the {0} stopped on conflicts in:\n{1}\n  Resolve each one "
                "(edit it, then git add <path>), run push_guard.py --check-index again, "
                "and continue the {0} only on exit 0.".format(progress[0], shown)
            )
        return EXIT_FOREIGN_INDEX
    staged = _staged()
    if progress is None:
        if staged:
            return _report_staged(staged, git_dir)
        if report:
            print("STATUS: INDEX MATCHES HEAD")
        return 0
    op, own = progress
    foreign = [(s, p) for s, p in staged if own is None or p not in own]
    if foreign and own is not None:
        return _report_staged(
            foreign, git_dir, lead="outside the {} in progress, ".format(op), op=op
        )
    if report and own is not None:
        print("STATUS: CLEAN ({} in progress; only its own paths are staged)".format(op))
        return 0
    err(
        "REFUSED: a {0} is in progress. Finish it (git {0} --continue) or abort "
        "it (git {0} --abort), then re-run.".format(op)
    )
    return EXIT_FOREIGN_INDEX


def _branch_ref():
    """refs/heads/<b> that HEAD points at (refuses a detached HEAD)."""
    ref = _head_ref()
    if not ref.startswith("refs/heads/"):
        err("REFUSED: HEAD is detached; check out your feature branch first.")
        raise Refused()
    return ref


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


def _git_version():
    out = _ask(["git", "version"])[1]
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _hooks_runnable():
    """True when this git runs hooks by name; refuses when it cannot but hooks exist."""
    version = _git_version()
    if version >= _HOOK_RUN_GIT:
        return True
    # --git-path resolves through core.hooksPath (relative to this directory),
    # so it names the hook git would run, wherever the hooks live.
    present = [
        name
        for name in ("prepare-commit-msg", "commit-msg")
        if os.path.exists(_git_paths("hooks/" + name)[0])
    ]
    if present:
        err(
            "REFUSED: git {}.{} cannot run the repository's commit message hooks for a "
            "squash (git hook run needs git {}.{} or later). Upgrade git and re-run.".format(
                *version, *_HOOK_RUN_GIT
            )
        )
        raise Refused()
    return False


def _stripspace(data, comments):
    args = ["git", "stripspace"] + (["--strip-comments"] if comments else [])
    return _ask(args, raw=True, input=data)[1]


def _cleanup_mode():
    rc, mode = _ask(["git", "config", "--get", "commit.cleanup"], answers=(0, 1))
    mode = mode if rc == 0 and mode else "default"
    if mode not in ("default", "strip", "whitespace", "verbatim", "scissors"):
        err("REFUSED: commit.cleanup is {!r}, which git commit rejects too.".format(mode))
        raise Refused()
    return mode


def _squash_message_file(git_dir):
    """The guard's own copy of the squash message while it is hooked and committed."""
    return os.path.join(git_dir, "push_guard-squash-{}.msg".format(os.getpid()))


def _hook_message(git_dir, message, tree):
    """``message`` (bytes) through the message hooks and cleaned as ``git commit -F`` does.

    prepare-commit-msg (source ``message``) and commit-msg run on it with an
    index of the squash's tree and GIT_EDITOR=":", as for any commit; then
    commit.cleanup applies (comments are stripped only when it is ``strip``).
    Returns the final bytes, or raises Refused (a hook rejected it, or it is
    empty).
    """
    mode = _cleanup_mode()
    text = message if mode == "verbatim" else _stripspace(message, comments=False)
    if not text.strip():
        err("REFUSED: the squash message is empty.")
        raise Refused()
    path = _squash_message_file(git_dir)
    index = os.path.join(git_dir, "push_guard-squash-{}.index".format(os.getpid()))
    _write_git_file(path, text)
    try:
        if _hooks_runnable():
            env = {"GIT_INDEX_FILE": index}
            _ask(["git", "read-tree", tree], env=env)
            env["GIT_EDITOR"] = ":"
            for hook, hook_args in (
                ("prepare-commit-msg", [path, "message"]),
                ("commit-msg", [path]),
            ):
                res = _interactive(
                    ["git", "hook", "run", "--ignore-missing", hook, "--", *hook_args], env
                )
                if res.timed_out or res.rc != 0:
                    err(
                        "REFUSED: the {} hook rejected the squash message ({}); its "
                        "output is above. Nothing was moved.".format(hook, _outcome(res))
                    )
                    raise Refused()
        with open(path, "rb") as handle:
            hooked = handle.read()
    except OSError as exc:
        err("REFUSED: cannot read the hooked squash message: {}".format(exc))
        raise Refused()
    finally:
        _unlink(path)
        _unlink(index)
    final = hooked if mode == "verbatim" else _stripspace(hooked, comments=mode == "strip")
    if not final.strip():
        err("REFUSED: the squash message is empty after its hooks and commit.cleanup.")
        raise Refused()
    return final


def _squash(base, ref, pre, pinned, ahead, git_dir, message, vouched):
    """Squash the branch to one commit of ``pre``'s tree on ``pinned``; return its sha.

    ``pre``, ``pinned`` and ``ahead`` (the commits between them) are what the
    history checks vetted.  Every path the squash changes must be accounted
    for (_unrecorded) or named (``vouched``).  The commit object is made by
    ``commit-tree`` from ``pre``'s tree (never an index), signed when
    commit.gpgSign asks, with the message through the message hooks.  It is
    recorded first; then the branch moves exactly once, by compare-and-swap
    from ``pre`` to the new commit, so an interrupt anywhere leaves it at one
    or the other.
    Why: references/rationale.md.
    """
    if pre == pinned:
        err("REFUSED: HEAD is origin/{} itself — there is nothing to squash.".format(base))
        raise Refused()
    tree = _commit_info(pre)[0]
    if tree == _commit_info(pinned)[0]:
        err(
            "REFUSED: the branch changes nothing against origin/{}; nothing to squash.".format(base)
        )
        raise Refused()
    changed = _changed(pinned, tree)
    unknown = _unrecorded(git_dir, ref, changed, ahead, pinned, pre, vouched)
    if unknown:
        _refuse_unrecorded(unknown, "the branch", git_dir, base, "squash")
    if _sha(ref) != pre:
        err("REFUSED: {} moved while the guard ran; re-run.".format(ref))
        raise Refused()

    text = _hook_message(git_dir, message, tree)
    rc, sign = _ask(["git", "config", "--bool", "commit.gpgSign"], answers=(0, 1))
    argv = ["git", "commit-tree", tree, "-p", pinned]
    if rc == 0 and sign == "true":
        argv.append("-S")
    path = _squash_message_file(git_dir)
    _write_git_file(path, text)
    try:
        res = _interactive(argv + ["-F", path])
    finally:
        _unlink(path)
    squashed = res.out.decode("ascii", "replace").strip()
    if res.timed_out or res.rc != 0 or not squashed:
        err(
            "REFUSED: git commit-tree failed ({}); its output is above. Nothing was moved.".format(
                _outcome(res)
            )
        )
        raise Refused()

    record_built(git_dir, squashed, changed)
    now = _move_ref(ref, squashed, pre, "push_guard: squash onto origin/" + base)
    if now != squashed:
        err(
            "REFUSED: {} could not be moved from {} to the squash (it is now {}); "
            "it was left alone, and the squash commit {} is unreferenced.".format(
                ref, pre[:12], now[:12], squashed[:12]
            )
        )
        raise Refused()
    rc, still = _ask(["git", "symbolic-ref", "--quiet", "HEAD"], answers=(0, 1))
    if rc != 0 or still != ref:
        err(
            "WARNING: HEAD moved to {} during the squash; your branch {} was "
            "updated — check it out.".format(still or "a detached commit", ref)
        )
    print("before squash:   " + pre[:12])
    print("HEAD:            " + squashed[:12])
    return squashed


def _read_message(path):
    """The message bytes (a UTF-8 BOM dropped), or raises Refused naming the file and the fix."""
    remedy = "Write the message (subject line first) to {} and re-run.".format(path)
    try:
        with open(path, "rb") as handle:
            message = handle.read()
    except OSError as exc:
        err("REFUSED: cannot read the squash message ({}). {}".format(exc, remedy))
        raise Refused()
    if message.startswith(b"\xef\xbb\xbf"):
        message = message[3:]
    if not message.strip():
        err("REFUSED: the squash message {} is empty. {}".format(path, remedy))
        raise Refused()
    return message


def _top_relative(path, top):
    """``path`` as the caller named it, relative to ``top`` (real paths both), or None.

    Only the parent directories are resolved: the named entry itself may be a
    tracked symlink.  None when it lies outside ``top`` (or on another drive).
    """
    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    real = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(absolute)
    try:
        rel = os.path.relpath(real, top)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _named_paths(paths, top):
    """{top-relative bytes: the name as given}, or raises UsageError for a path outside.

    A name is a path from this directory, or ``:(top,literal)<path>`` from the
    top: the form refusals print, so a listed path is named back as printed.
    """
    named = {}
    for path in paths:
        given = path
        if path.startswith(_TOP_LITERAL):
            rel = _literal_relative(path[len(_TOP_LITERAL) :], given)
        else:
            rel = _top_relative(path, top)
        if rel is None:
            err("ERROR: {!r} is not inside this repository ({}).".format(given, top))
            raise UsageError()
        named[os.fsencode(rel)] = given
    return named


def _literal_relative(rest, given):
    """The top-relative path a ``:(top,literal)`` name spells, or None when it leaves the top.

    Read as written, never resolved: it is already top-relative, so a parent
    that is now a symlink in the worktree still names the path git lists.
    Raises UsageError for a name that names nothing.
    """
    if not rest.strip():
        err("ERROR: {!r} names nothing; name each path.".format(given))
        raise UsageError()
    if os.path.isabs(rest) or os.path.splitdrive(rest)[0]:
        return None
    text = rest.replace(os.sep, posixpath.sep)
    if os.pardir in text.split(posixpath.sep):
        return None
    return posixpath.normpath(text)


def _index_entries():
    """{top-relative path bytes: (mode, sha, stage) of each entry} in the shared index.

    ``--full-name``: without it ls-files names entries relative to this
    directory, and a lookup by top-relative path would miss from a subdirectory.
    """
    out = _ask(["git", "ls-files", "-s", "-z", "--full-name", "--", ":(top)"], raw=True)[1]
    entries: dict[bytes, tuple] = {}
    for entry in out.split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        if tab:
            entries[name] = entries.get(name, ()) + (tuple(meta.decode("ascii").split(" ")),)
    return entries


def _worktree_matches(rel, top, entry):
    """True when the worktree copy of ``rel`` holds exactly the staged content."""
    mode, sha = entry
    path = os.path.join(top, os.fsdecode(rel))
    if mode == "160000":
        # A gitlink: a checked-out submodule shows its commit; one that is not
        # checked out shows none, so the staged commit is the only copy.
        if not os.path.exists(os.path.join(path, ".git")):
            return True
        rc, out = _ask(["git", "-C", path, "rev-parse", "--verify", "-q", "HEAD"], answers=(0, 1))
        return rc == 0 and out == sha
    try:
        if mode == "120000":
            target = os.readlink(path)
            data = os.fsencode(target) if isinstance(target, str) else target
            return _ask(["git", "hash-object", "--stdin"], input=data)[1] == sha
        if os.path.islink(path) or not os.path.isfile(path):
            return False
    except OSError:
        return False
    return _ask(["git", "hash-object", "--", path])[1] == sha


def _private_index(git_dir, rev, env):
    """Seed the index at ``env``'s GIT_INDEX_FILE with ``rev``'s tree.

    A copy of the shared index first, so its stat data spares re-hashing the
    worktree; ``read-tree --reset`` then makes the entries ``rev``'s and keeps
    the stat data only where the content is the same.
    """
    try:
        # copy2, never copyfile: the index's own mtime is how git tells a
        # racily clean entry (a file edited in the second it was staged) from
        # a clean one, and a fresh mtime would make it trust the stale entry.
        shutil.copy2(_git_paths("index")[0], env["GIT_INDEX_FILE"])
    except OSError:
        pass
    _ask(["git", "read-tree", "--reset", rev], env=env)


def _commit_by_name(paths, message, message_file, amend, git_dir, base_arg=""):
    """Commit (or amend) exactly the named paths; return 0 or an exit code.

    Refuses before touching anything when an operation is mid-way, when any
    entry is unmerged, or when anything is staged that is not literally one
    of the named paths (a directory does not name the entries under it).
    The commit is built in a private index seeded from HEAD: each named
    path's worktree copy is added (``-f`` only for a file, never for a
    directory), except that a staged change the worktree cannot show
    (``git rm --cached``, ``update-index --chmod``) is kept.  ``git commit``
    commits that index, hooks and signing included; the shared index changes
    only after it lands, for the committed paths alone, and only where no one
    staged anything since this run read it.  The commit landed when git says
    so AND HEAD is what was built: its parents, its author, and its tree up to
    the named paths (a hook may reformat those).  Anything else is taken back
    with a compare-and-swap.  Only a landed commit is recorded, so a commit a
    hook rejected leaves no path recorded.
    """
    rc = _check_index_for_commit()
    if rc:
        return rc
    top = os.path.realpath(_top())
    named = _named_paths(paths, top)
    # Read first: the sync after landing levels only entries still as read here.
    before = _index_entries()
    staged = {p: s for s, p in _staged()}
    unnamed = [(s, p) for p, s in staged.items() if p not in named]
    if unnamed:
        return _report_staged(unnamed, git_dir, lead="not among the paths you named, ")
    branch = _head_ref()
    pre = _sha("HEAD")
    pre_tree, pre_parents, pre_author = _commit_info(pre)
    # The paths this commit changes are diffed against its parent: HEAD or, for an
    # amend, HEAD's first parent (the empty tree when HEAD is a root commit).
    if not amend:
        diff_base = pre
    elif pre_parents:
        diff_base = pre_parents[0]
    else:
        diff_base = _empty_tree()
    if amend:
        # The base as last fetched (an amend fetches nothing), so renames the
        # base made and published paths count exactly as in the push check.
        base, pinned = _local_base(base_arg)
        unknown = _unrecorded(git_dir, branch, _own_diff(pre), [pre], pinned, pre, named)
        if unknown:
            _refuse_unrecorded(unknown, "HEAD ({})".format(pre[:12]), git_dir, base, "amend")
    author = pre_author if amend else _ident(_ask(["git", "var", "GIT_AUTHOR_IDENT"])[1])

    tag = "{}-{}".format(os.getpid(), time.time_ns())
    env = {"GIT_INDEX_FILE": os.path.join(git_dir, "push_guard-index-" + tag)}
    msg_path = os.path.join(git_dir, "push_guard-message-{}.txt".format(tag))
    try:
        _private_index(git_dir, pre, env)
        files: list[str] = []
        dirs: list[str] = []
        for rel in named:
            spec = _TOP_LITERAL + os.fsdecode(rel)
            letter = staged.get(rel)
            if letter == b"D":
                _ask(["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", spec], env=env)
                continue
            entry = before[rel][0][:2] if letter is not None and rel in before else None
            if entry is not None and _worktree_matches(rel, top, entry):
                info = "{} {}\t".format(*entry).encode("ascii") + rel + b"\0"
                _ask(["git", "update-index", "-z", "--index-info"], env=env, input=info)
                continue
            on_disk = os.path.join(top, os.fsdecode(rel))
            is_dir = os.path.isdir(on_disk) and not os.path.islink(on_disk)
            (dirs if is_dir else files).append(spec)
        if files:
            _ask(["git", "add", "-A", "-f", "--", *files], env=env)
        if dirs:
            _ask(["git", "add", "-A", "--", *dirs], env=env)
        built = _ask(["git", "write-tree"], env=env)[1]
        if built == pre_tree and not amend:
            err("REFUSED: the named paths hold no change against HEAD; nothing was committed.")
            return EXIT_REFUSED

        argv = ["git", "commit", "--quiet"] + (["--amend"] if amend else [])
        if message_file is not None:
            argv += ["-F", os.path.abspath(message_file)]
        elif message is not None:
            _write_git_file(msg_path, message + b"\n")
            argv += ["-F", msg_path]
        else:
            argv.append("--no-edit")
        res = _interactive(argv, env)
    finally:
        _unlink(env["GIT_INDEX_FILE"])
        _unlink(env["GIT_INDEX_FILE"] + ".lock")
        _unlink(msg_path)

    head = _sha("HEAD")
    tree, parents, head_author = _commit_info(head)
    want = pre_parents if amend else [pre]
    if res.timed_out or res.rc != 0:
        if head == pre:
            err(
                "REFUSED: git commit {} — nothing was committed. {}".format(
                    "timed out" if res.timed_out else "failed (exit {})".format(res.rc),
                    _tail(res.out.decode("utf-8", "replace")) or "Its output is above.",
                )
            )
        else:
            err(
                "REFUSED: git commit reported {} but HEAD moved to {}; read git show "
                "{} before anything else.".format(_outcome(res), head[:12], head[:12])
            )
        return EXIT_REFUSED
    swept = _outside(built, tree, named)
    if parents != want or head_author != author or swept:
        _take_back(
            head, built, named, swept, pre=pre, parents=parents, placed=parents == want, amend=amend
        )
        return EXIT_REFUSED
    try:
        record_built(git_dir, head, _changed(diff_base, tree))
    except Refused:
        err(
            "WARNING: the commit landed but is not recorded, so the push check will "
            "list its paths; name them after -- there."
        )
    sync = _changed(pre_tree, tree) | set(named)
    # Staged by someone else since this run read the index: theirs, left alone.
    now = _index_entries() if sync else {}
    restaged = sorted(p for p in sync if before.get(p) != now.get(p))
    sync -= set(restaged)
    # Nothing to level (an amend of the message alone): an empty pathspec list
    # would make the reset below a full reset of the shared index.
    rc, stderr = 0, ""
    if sync:
        listed = b"".join(os.fsencode(_TOP_LITERAL) + p + b"\0" for p in sorted(sync))
        rc, _, stderr = run(
            ["git", "reset", "-q", head, "--pathspec-from-file=-", "--pathspec-file-nul"],
            input=listed,
        )
    if restaged:
        err(
            "WARNING: the commit landed; these paths were staged again while it "
            "ran, so the index keeps that:\n{}".format(_indented(restaged))
        )
    if rc != 0:
        err(
            "WARNING: the commit landed, but the index still shows the committed "
            "paths as before ({}); git reset -q -- <path> brings them level.".format(
                _tail(stderr) if rc is not None else "timed out"
            )
        )
    print("HEAD:            " + head[:12])
    return 0


def _covered(path, named):
    """True when ``path`` is a named path or lies under a named directory."""
    return any(n in (path, b".") or path.startswith(n + b"/") for n in named)


def _outside(old, new, named):
    """Paths (sorted bytes) that differ between two trees and are not named."""
    return sorted(p for p in _changed(old, new) if not _covered(p, named))


def _take_back(head, built, named, swept, *, pre, parents, placed, amend):
    """Refuse a commit that is not the one built; move HEAD back over it when that is safe.

    ``pre`` is HEAD as this run read it, ``parents`` the commit's own and
    ``placed`` whether they are the ones it was built for.  Where HEAD was is
    the reflog's previous entry, and only two shapes are unambiguous: HEAD
    sits where it was built and that entry is ``pre``; or git built on (or,
    for an amend, replaced) a commit that landed in between, which that entry
    is, holding a change outside the named paths.  A commit a hook stacked
    on top, or rewrote again, is left for the reader.  The move is a
    compare-and-swap from ``head``, so a commit that landed after it is never
    undone.
    """
    rc, prev = _ask(["git", "rev-parse", "--verify", "-q", "HEAD@{1}"], answers=(0, 1))
    if rc != 0 or (placed and prev != pre):
        prev = ""
    elif not placed:
        prev_tree, prev_parents, _ = _commit_info(prev)
        follows = prev_parents == parents if amend else parents[:1] == [prev]
        if not follows or not _outside(prev_tree, built, named):
            prev = ""
    if swept:
        why = "a hook added paths you did not name:\n" + _indented(swept[:_LISTED_PATHS])
    else:
        why = "its parents or author differ: another commit landed at the same time, or a hook rewrote it"
    reason = "push_guard: take back a commit that is not the one built"
    if prev and _move_ref("HEAD", prev, head, reason) == prev:
        err(
            "REFUSED: the commit git made ({}) is not the one that was built ({}).\n"
            "  It was taken back: HEAD is {} again, and the index and worktree are "
            "as they were. git show {} shows what it held; re-run once the cause "
            "is gone.".format(head[:12], why, prev[:12], head[:12])
        )
        return
    err(
        "REFUSED: HEAD ({}) is not the commit that was built ({}). Read git show {} "
        "before anything else.".format(head[:12], why, head[:12])
    )


def _check_index_for_commit():
    """--commit/--amend's preconditions on state: no operation mid-way, no conflict."""
    progress = _in_progress()
    if progress is not None:
        err(
            "REFUSED: a {0} is in progress; finish it (git {0} --continue) or "
            "abort it first.".format(progress[0])
        )
        return EXIT_FOREIGN_INDEX
    if _unmerged():
        err(
            "REFUSED: the index holds unresolved conflicts; resolve them before "
            "any commit, and never commit a file that still carries conflict markers."
        )
        return EXIT_FOREIGN_INDEX
    return 0


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


if __name__ == "__main__":
    sys.exit(main())

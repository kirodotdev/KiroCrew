"""Neutralize git's config-defined hooks (and submodule recursion) for one host-side call.

Git 2.54 runs hooks named in config, not just hook files::

    [hook "x"]
        command = <any shell command>
        event = pre-commit

``-c core.hooksPath=<devnull>`` only moves the hook DIRECTORY, so these still run. A
repository's own ``.git/config`` is writable by whoever edits the tree, so a host-side git
call over an agent-writable tree would run that command outside the sandbox. They fire on
ordinary porcelain: ``add``, ``commit``, ``checkout``, ``push``, ``fetch``, ``update-ref``,
and even ``status`` when it refreshes the index (``post-index-change``).

There is no switch that turns them all off. ``hook.<event>.enabled=false`` (git 2.55+) is
ignored when the same ``<event>`` is also used as a hook name, which the repository can
arrange. ``hook.<name>.enabled=false`` works on every version that has config hooks, but only
for a name we know. So each call first lists the names git will see and disables each one by
name. Older git ignores the extra keys.

Submodules
----------

A ``status``/``diff`` spawns a CHILD git in each initialized submodule it checks, that child
spawns one in each of ITS submodules, and so on -- each reading its OWN agent-writable config.
So every initialized level's hook names are listed and disabled too; the
``-c hook.<name>.enabled=false`` flags reach a child -- and a grandchild -- through
``GIT_CONFIG_PARAMETERS`` (verified two levels deep on git 2.55). Status recursion itself is
left intact: it is NOT suppressed with ``diff.ignoreSubmodules`` (that key is overridable by a
repo's own ``submodule.<name>.ignore`` AND would blind callers -- e.g. the auto-improvement
watcher's durability check -- to real submodule changes).

A DEINITIALIZED submodule has no working tree, so ``status``/``diff`` never recurse into it and
the by-name scan cannot see its config; only a recursive ``fetch``/``checkout`` would read its
gitdir. Those are pinned off instead (:data:`_SUBMODULE_RECURSION_PINS`), which also covers the
awkward gitdir layouts (nested, slash-named, ``includeIf``). ``push.recurseSubmodules`` is
already pinned at the auto_improvement call site.

How the scan runs
-----------------

Each scan git call goes through a *runner*. The default runner spawns git directly, for callers
whose real call is unsandboxed. A caller whose REAL git call runs inside the OS sandbox passes a
runner built from its own ``sandboxed_spawn_argv`` (:func:`config_hook_disable_args_sandboxed`),
so the scan is confined exactly like the call it protects and a test that stubs that caller's
``sandboxed_spawn_argv`` answers the scan too. An async caller uses
:func:`config_hook_disable_args_sandboxed_async`, which runs the whole sync scan in the
subprocess executor -- the module never hard-codes the sandbox chokepoint, so no bare off-loop
hop to it exists; each spawn creates and unlinks its launcher within the one worker call, so a
cancelled awaiter cannot abandon one.

The list is read just before the call, by a separate process, so a writer running at the same
moment can add a name in between -- the same limit the ``.git/info/attributes`` pin in
:mod:`kiro_crew.apps.builtins.auto_improvement.spine.git_safety` has.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

__all__ = [
    "ConfigHookScanError",
    "config_hook_disable_args",
    "config_hook_disable_args_sandboxed",
    "config_hook_disable_args_sandboxed_async",
]

#: More names than this is refused rather than passed on: no real setup has this many, and each
#: one costs two argv entries.
_MAX_HOOK_NAMES = 256

#: Individual names longer than this (UTF-8 bytes) cannot be passed as ``-c`` arguments without
#: risking E2BIG. Reject them early.
_MAX_HOOK_NAME_BYTES = 512

#: Ceiling on how many repositories (superproject's submodules, their submodules, ...) the walk
#: scans. A pathological nest cannot turn one git call into unbounded scan subprocesses.
_MAX_SUBMODULE_REPOS = 256

_SCAN_TIMEOUT_SECS = 10

#: Stop git from spawning a child git in a submodule gitdir we did NOT scan by name: a recursive
#: fetch/checkout would otherwise read a submodule's config (including a deinitialized
#: submodule's gitdir, which has no working tree and so is invisible to the by-name scan) and
#: run its hooks. ``push.recurseSubmodules`` is pinned at the auto_improvement site. Placed
#: before the subcommand like the hook-disable flags. ``diff.ignoreSubmodules`` is deliberately
#: NOT pinned (overridable per submodule, and it would blind status to real changes).
_SUBMODULE_RECURSION_PINS = ["-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false"]

#: Bound once, so a stand-in a caller's tests install for ``subprocess.run`` (to fake that
#: caller's own git) does not also answer this scan.
_run = subprocess.run

#: Runs one scan git argv and returns ``(returncode, stdout, stderr)``. May raise
#: ``FileNotFoundError`` (git absent), ``RuntimeError`` (sandbox could not confine the spawn),
#: or another ``OSError``/``subprocess.SubprocessError``.
_ScanRunner = Callable[[list[str]], "tuple[int, bytes, bytes]"]

#: The :func:`kiro_crew.sandbox.sandboxed_spawn_argv` contract.
_SpawnArgv = Callable[..., "tuple[list[str], dict[str, str], str | None]"]


class ConfigHookScanError(RuntimeError):
    """The hook names could not be listed safely, so the git call must not run."""


def _add_hook_name(name: str, names: list[str], seen: set[str]) -> None:
    """Validate one hook name and append it (deduping via ``seen``).

    One home for the ``=``, byte-length and count rules the superproject and submodule scans
    share. Raises :class:`ConfigHookScanError` for a name that could not be disabled safely.
    """
    if name in seen:
        return
    if "=" in name:
        raise ConfigHookScanError(
            f"git hook name {name[:80]!r} contains '=' and cannot be disabled"
        )
    name_bytes = name.encode("utf-8", "surrogateescape")
    if len(name_bytes) > _MAX_HOOK_NAME_BYTES:
        raise ConfigHookScanError(
            f"git hook name is {len(name_bytes)} bytes (limit {_MAX_HOOK_NAME_BYTES})"
        )
    seen.add(name)
    names.append(name)
    if len(names) > _MAX_HOOK_NAMES:
        raise ConfigHookScanError(
            f"git config defines {len(names)} hook names (limit {_MAX_HOOK_NAMES})"
        )


def _default_runner(env: Mapping[str, str] | None) -> _ScanRunner:
    """A runner that spawns git directly, for callers whose real call is unsandboxed."""
    env_dict = dict(env) if env is not None else None

    def run(argv: list[str]) -> tuple[int, bytes, bytes]:
        proc = _run(
            argv,
            env=env_dict,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_SCAN_TIMEOUT_SECS,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return run


def _add_names_from_config_output(out: bytes, names: list[str], seen: set[str]) -> None:
    """Parse ``config -z --name-only --get-regexp ^hook.`` output and add each hook name."""
    for raw in out.split(b"\0"):
        key = raw.decode("utf-8", "surrogateescape")  # surrogateescape: non-UTF-8 round-trips
        if not key.startswith("hook."):
            continue
        rest = key[len("hook.") :]
        if "." not in rest:
            continue  # `hook.jobs`: a setting, not a named hook
        _add_hook_name(rest[: rest.rindex(".")], names, seen)


def _submodule_hook_names(
    cwd: str | os.PathLike[str], git: str, run: _ScanRunner, names: list[str], seen: set[str]
) -> None:
    """Add the hook names of every INITIALIZED gitlinked submodule reachable from ``cwd``.

    Recursive (a submodule of a submodule too). Each submodule is scanned by running git IN its
    own working tree (``git -C <dir> config``), covering absorbed and non-absorbed layouts alike.
    A submodule with no checked-out working tree, or any git error running IN it, is skipped: no
    child git runs there on ``status``/``diff``, so there is nothing to disable (a recursive
    fetch/checkout, which could reach a deinitialized gitdir, is pinned off separately). The walk
    bounds the repositories RETAINED (scanned + pending) by :data:`_MAX_SUBMODULE_REPOS`,
    deduping and cycle-guarding by real path, and RAISES rather than returning a partial list
    when the bound (or a name rule) is exceeded.
    """

    def _exec(argv: list[str]) -> tuple[int, bytes, bytes] | None:
        # Best-effort below the superproject: a submodule we cannot run git in is one no child
        # runs in either. (A sandbox refusal cannot reach here -- the superproject scan uses the
        # same runner and would have failed closed first.)
        try:
            return run(argv)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return None

    top = _exec(
        [git, "-C", os.fspath(cwd), "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"]
    )
    if top is None or top[0] != 0:
        return
    # Strip only git's terminating newline, not real whitespace: a work tree whose own path ends
    # in a space must still resolve, or its submodules are silently dropped.
    toplevel = top[1].decode("utf-8", "surrogateescape").removesuffix("\n")
    if not toplevel:
        return

    seen_repos: set[str] = {os.path.realpath(toplevel)}
    stack: list[str] = [toplevel]
    while stack:
        repo_dir = stack.pop()
        listed = _exec([git, "-C", repo_dir, "-c", "core.fsmonitor=false", "ls-files", "-s", "-z"])
        if listed is None or listed[0] != 0:
            continue
        for entry in listed[1].split(b"\0"):
            if not entry.startswith(b"160000 "):
                continue  # only gitlinks (submodules)
            tab = entry.find(b"\t")  # format: "<mode> <oid> <stage>\t<path>"
            if tab == -1:
                continue
            rel = entry[tab + 1 :].decode("utf-8", "surrogateescape")
            sub_dir = os.path.join(repo_dir, rel)
            sub_real = os.path.realpath(sub_dir)
            if sub_real in seen_repos:
                continue  # already scanned/queued (dedupe), or a cycle back to an ancestor
            if len(seen_repos) >= _MAX_SUBMODULE_REPOS:
                raise ConfigHookScanError(
                    f"git config spans more than {_MAX_SUBMODULE_REPOS} nested repositories"
                )
            seen_repos.add(sub_real)
            cfg = _exec(
                [
                    git,
                    "-C",
                    sub_dir,
                    "config",
                    "--includes",
                    "-z",
                    "--name-only",
                    "--get-regexp",
                    r"^hook\.",
                ]
            )
            if cfg is None:
                continue  # cannot run git in this submodule -> no child runs there either
            if cfg[0] not in (0, 1):
                continue
            _add_names_from_config_output(cfg[1], names, seen)
            stack.append(sub_dir)  # recurse: this submodule can itself contain gitlinks


def config_hook_names(
    cwd: str | os.PathLike[str],
    *,
    git: str = "git",
    env: Mapping[str, str] | None = None,
    runner: _ScanRunner | None = None,
) -> list[str]:
    """Every ``hook.<name>.*`` name git would read: the superproject (all scopes/includes) plus
    every initialized gitlinked submodule, recursively.

    ``git`` and ``env`` should be what the real call uses. ``runner`` executes each scan git
    argv; the default spawns git directly. A ``cwd`` that is not a directory, or a ``git`` that
    cannot be found, returns ``[]``. Raises :class:`ConfigHookScanError` when the listing fails,
    a name contains ``=``, a name exceeds :data:`_MAX_HOOK_NAME_BYTES`, there are more than
    :data:`_MAX_HOOK_NAMES` names, or more than :data:`_MAX_SUBMODULE_REPOS` repositories; a
    runner's own spawn failure propagates unchanged for the caller to map like its real call.
    """
    if not os.path.isdir(cwd):
        return []
    run = runner if runner is not None else _default_runner(env)
    argv = [
        git,
        "-C",
        os.fspath(cwd),
        "-c",
        "core.fsmonitor=false",
        "config",
        "-z",
        "--name-only",
        "--get-regexp",
        r"^hook\.",
    ]
    try:
        rc, out, err = run(argv)
    except FileNotFoundError:
        return []  # the real call spawns the same name and cannot run either
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigHookScanError(f"could not list git hook config: {exc}") from exc
    if rc not in (0, 1):  # 0 found, 1 no match; anything else is an unreadable config
        tail = err.decode("utf-8", "replace").strip()[-200:]
        raise ConfigHookScanError(f"could not list git hook config ({rc}): {tail}")
    names: list[str] = []
    seen: set[str] = set()
    _add_names_from_config_output(out, names, seen)
    _submodule_hook_names(cwd, git, run, names, seen)
    return names


def _disable_args_for_names(names: list[str]) -> list[str]:
    """Hook-disable flags for ``names`` followed by the submodule-recursion pins."""
    args: list[str] = []
    for name in names:
        args += ["-c", f"hook.{name}.enabled=false"]
    args += _SUBMODULE_RECURSION_PINS
    return args


def config_hook_disable_args(
    cwd: str | os.PathLike[str],
    *,
    git: str = "git",
    env: Mapping[str, str] | None = None,
    runner: _ScanRunner | None = None,
) -> list[str]:
    """Argv entries to place before the subcommand: ``-c hook.<name>.enabled=false`` for each
    hook (superproject and initialized submodules), then the submodule-recursion pins. Raises
    like :func:`config_hook_names`.
    """
    return _disable_args_for_names(config_hook_names(cwd, git=git, env=env, runner=runner))


def _sandboxed_runner(
    spawn_argv: _SpawnArgv, *, mode: str, env: Mapping[str, str] | None, cwd: str
) -> _ScanRunner:
    """A runner that wraps each scan argv with the caller's ``spawn_argv`` and runs it, sync.

    Each spawn's launcher is created AND unlinked inside this one call, so there is no tuple held
    across an ``await`` for a cancellation to abandon -- an async caller may reach this through
    ``run_in_executor`` without the ``shielded_prepare_off_loop`` dance.
    """

    def run(argv: list[str]) -> tuple[int, bytes, bytes]:
        # RuntimeError from spawn_argv (no sandbox backend and no opt-in) propagates: the caller
        # maps it like a real-call spawn failure (fail closed).
        wrapped, scrubbed, cleanup = spawn_argv(argv, mode=mode, env=env)
        scrubbed["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = _run(
                wrapped,
                cwd=cwd,
                env=scrubbed,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_SCAN_TIMEOUT_SECS,
                check=False,
            )
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass
        return proc.returncode, proc.stdout, proc.stderr

    return run


def config_hook_disable_args_sandboxed(
    cwd: str | os.PathLike[str],
    *,
    spawn_argv: _SpawnArgv,
    mode: str,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Like :func:`config_hook_disable_args`, for a SYNC caller whose real git call is sandboxed.

    Each scan git call is wrapped by the caller's own ``spawn_argv``
    (:func:`kiro_crew.sandbox.sandboxed_spawn_argv`) at the caller's ``mode``/``env`` and spawned,
    so the scan is confined like the call it protects and a test that stubs the caller's
    ``spawn_argv`` answers the scan. Fail-closed like :func:`config_hook_disable_args`.
    """
    runner = _sandboxed_runner(spawn_argv, mode=mode, env=env, cwd=os.fspath(cwd))
    return config_hook_disable_args(cwd, git="git", env=env, runner=runner)


async def config_hook_disable_args_sandboxed_async(
    cwd: str | os.PathLike[str],
    *,
    spawn_argv: _SpawnArgv,
    mode: str,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Async form of :func:`config_hook_disable_args_sandboxed` for a caller on the event loop.

    Runs the whole sync scan in the subprocess executor. This module never names the sandbox
    chokepoint (the caller injects ``spawn_argv``), so there is no bare off-loop hop to it; and
    each spawn creates and unlinks its launcher inside the worker call, so a cancelled awaiter
    cannot abandon one.
    """
    import asyncio
    import functools

    from kiro_crew.executors import subprocess_executor

    return await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        functools.partial(
            config_hook_disable_args_sandboxed, cwd, spawn_argv=spawn_argv, mode=mode, env=env
        ),
    )

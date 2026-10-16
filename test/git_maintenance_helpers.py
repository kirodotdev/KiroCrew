"""Keep git's auto maintenance off in test environments.

A commit otherwise starts a detached ``git maintenance run --auto`` that outlives
the call and adds and removes lock files under .git, so a test that walks,
snapshots or copies the repo right after can see a file vanish.

The repo-root ``conftest.py`` loads this module by path, so the floor reaches
every testpath, ``src/kiro_crew/apps/builtins`` included. It does two things
for every session and test:

* points ``GIT_CONFIG_GLOBAL`` at a session-owned file that ends with
  ``GIT_MAINTENANCE_OFF_CONFIG`` (see :func:`git_global_config_text`). Every git
  that reads global config sees it, including a ``receive-pack`` that a push
  over the local transport starts in the remote, and git that code under test
  runs with its own env built over ``os.environ``;
* appends the same keys through ``GIT_CONFIG_COUNT``
  (:func:`git_maintenance_off_entries`), which also holds for git whose
  ``GIT_CONFIG_GLOBAL`` a fixture pointed somewhere else.

A fixture helper that builds its own git env, sets its own
``GIT_CONFIG_COUNT`` and points ``GIT_CONFIG_GLOBAL`` away bypasses both, so it
applies the entries to its finished env dict:
``env.update(git_maintenance_off_entries(env))``. A push under such an env still
starts ``receive-pack`` in the remote without ``GIT_CONFIG_*`` (git's
``local_repo_env``) and with no global file, so the remote it pushes to also
writes ``GIT_MAINTENANCE_OFF_CONFIG`` into its own config.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

#: Config that turns auto maintenance off. ``gc.auto`` covers git older than
#: 2.29, whose commit runs ``gc --auto`` instead.
GIT_MAINTENANCE_OFF_CONFIG: tuple[tuple[str, str], ...] = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)


def _quoted(value: str) -> str:
    """*value* as a double-quoted git config value."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def git_global_config_text(includes: Iterable[str] = ()) -> str:
    """A global config file that includes *includes*, then turns auto maintenance off.

    The keys come after the includes, so they win over an included file. Git
    skips an include path that does not exist.
    """
    lines = [f"[include]\n\tpath = {_quoted(path)}\n" for path in includes]
    for key, value in GIT_MAINTENANCE_OFF_CONFIG:
        section, name = key.rsplit(".", 1)
        lines.append(f"[{section}]\n\t{name} = {value}\n")
    return "".join(lines)


def git_maintenance_off_entries(environ: Mapping[str, str]) -> dict[str, str]:
    """The env entries that turn git's auto maintenance off, or none if already off.

    Every key of ``GIT_MAINTENANCE_OFF_CONFIG`` is appended after any config
    entries the environment already carries (Git >= 2.31 reads
    GIT_CONFIG_COUNT), unless each one's last entry already has the off value.
    A malformed or negative count is reset to 0, as
    ``registry_pipeline.git_targets`` does: git rejects a malformed count, and
    a negative one would leave the entries unread.
    """
    try:
        count = int(environ.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    count = max(count, 0)
    last: dict[str, str | None] = {}
    for index in range(count):
        key = environ.get(f"GIT_CONFIG_KEY_{index}", "").lower()
        last[key] = environ.get(f"GIT_CONFIG_VALUE_{index}")
    if all(last.get(key) == value for key, value in GIT_MAINTENANCE_OFF_CONFIG):
        return {}
    entries: dict[str, str] = {}
    for offset, (key, value) in enumerate(GIT_MAINTENANCE_OFF_CONFIG):
        entries[f"GIT_CONFIG_KEY_{count + offset}"] = key
        entries[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    entries["GIT_CONFIG_COUNT"] = str(count + len(GIT_MAINTENANCE_OFF_CONFIG))
    return entries


def _is_git_argv(args: object) -> bool:
    """Whether a ``Popen`` *args* runs git (``git`` or ``git.exe`` by any path)."""
    if isinstance(args, (str, bytes, os.PathLike)):
        return False
    try:
        program = os.fsdecode(next(iter(args)))  # type: ignore[call-overload]
    except (StopIteration, TypeError):
        return False
    return os.path.basename(program).lower() in ("git", "git.exe")


def trace_git_spawns(monkeypatch, trace_dir: Path) -> Path:
    """Make every git this process spawns write a trace2 event file into *trace_dir*.

    ``GIT_TRACE2_EVENT`` is added to the env each git is started with, after
    the caller built it, so git run by a helper that strips ``GIT_*`` or builds
    its env from nothing is traced too. Setting the variable in ``os.environ``
    misses those helpers, which is how an earlier count missed 145 starts.
    Read the result with :func:`auto_maintenance_starts`.

    It replaces ``subprocess.Popen`` on the module, so it does not see git
    started by a caller that bound ``Popen`` itself (``from subprocess import
    Popen``), by a ``shell=True`` string command, or outside ``Popen``
    (``os.posix_spawn``, ``os.system``, pexpect). A ``git`` wrapper on ``PATH``
    sees those, but not a caller that runs git by absolute path or with a
    scrubbed ``PATH``.
    """
    import subprocess

    trace_dir.mkdir(parents=True, exist_ok=True)
    target = str(trace_dir)
    base = subprocess.Popen

    class _TracedPopen(base):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            if _is_git_argv(args):
                env = kwargs.get("env")
                env = dict(os.environ if env is None else env)
                env["GIT_TRACE2_EVENT"] = target
                kwargs["env"] = env
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _TracedPopen)
    return trace_dir


def auto_maintenance_starts(trace_dir: Path) -> list[list[str]]:
    """The argv of every ``maintenance run --auto`` or ``gc --auto`` git started.

    Reads the trace2 event files in *trace_dir* (one per git process when
    ``GIT_TRACE2_EVENT`` names a directory) and returns each ``child_start``
    whose argv runs auto maintenance or auto gc.
    """
    starts: list[list[str]] = []
    for path in sorted(trace_dir.iterdir()) if trace_dir.is_dir() else ():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("event") != "child_start":
                continue
            argv = [str(arg) for arg in event.get("argv") or ()]
            if "--auto" in argv and ({"maintenance", "gc"} & set(argv[1:3])):
                starts.append(argv)
    return starts

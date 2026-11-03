"""The launcher every Agent Plugin stdio server starts through.

``python -P -m kiro_crew.agent_plugins.launch <plugin> <server>``

A rebuild projects each stdio server of an ``on`` plugin as this command rather
than the plugin's own, so every harness gets the spec's launch rules (§9) the
same way, KAS included, which drops ``cwd`` from MCP entries. The launcher:

1. re-reads the owner's record, re-inspects that one plugin and refuses unless
   it is ``on``: valid and turned on by the owner;
2. creates the plugin's data folder, ``<powers home>/data/<plugin>/``;
3. expands ``${PLUGIN_ROOT}`` and ``${PLUGIN_DATA}`` once, without recursion, in
   ``args``, ``env`` and ``cwd`` (§9.2);
4. overlays the declared ``env`` on its own environment, then sets
   ``PLUGIN_ROOT`` and ``PLUGIN_DATA`` last (§9.1);
5. resolves ``command`` as one token (a bare name through ``PATH``, a ``./`` path
   against the root), changes to ``cwd`` (the root when omitted), and replaces
   itself with the server. Windows has no in-place exec, so there the server
   runs as a child on this process's stdio and its exit status is passed on.

A refusal prints one line on stderr naming the reason and exits 78
(``EX_CONFIG``); the server is never started.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from kiro_crew import platform_compat
from kiro_crew.agent_plugins import consent
from kiro_crew.agent_plugins.discovery import apply_consent, inspect_plugin
from kiro_crew.agent_plugins.model import StdioServer
from kiro_crew.agent_plugins.paths import is_plain_name, plugin_data_dir, powers_home

#: The module a projected server entry runs (``python -m``).
LAUNCH_MODULE = "kiro_crew.agent_plugins.launch"
_EXIT_REFUSED = 78
_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")


class LaunchRefused(Exception):
    """The server must not start; the message says why."""


@dataclass(frozen=True)
class Launch:
    """Everything needed to start one server, already expanded."""

    executable: str
    argv: list[str]
    env: dict[str, str]
    cwd: str


def expand(value: str, root: str, data: str) -> str:
    """*value* with each ``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}`` replaced once.

    A single left-to-right pass: text a replacement inserts is never scanned
    again, and any other ``${...}`` or ``$NAME`` stays literal (§9.2).
    """
    values = {"PLUGIN_ROOT": root, "PLUGIN_DATA": data}
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], value)


def _resolve_cwd(cwd: str | None, root: str, data: str) -> str:
    if cwd is None:
        return root
    if cwd.startswith("./"):
        return os.path.normpath(os.path.join(root, cwd[2:]))
    return os.path.normpath(expand(cwd, root, data))


def _resolve_command(command: str, root: str, path_env: str | None) -> str:
    if command.startswith("./"):
        resolved = os.path.realpath(os.path.join(root, command[2:]))
        try:
            inside = os.path.commonpath([resolved, root]) == root
        except ValueError:
            inside = False  # different drives on Windows
        if not inside or not os.path.isfile(resolved):
            raise LaunchRefused(f"its command {command!r} is not a file inside the plugin")
        return resolved
    found = shutil.which(command, path=path_env)
    if not found:
        raise LaunchRefused(f"its command {command!r} was not found on PATH")
    return found


def plan_launch(
    plugin: str,
    server: str,
    environ: dict[str, str],
    *,
    home: str | os.PathLike[str] | None = None,
    enabled: frozenset[str] | None = None,
) -> Launch:
    """The verified launch for *server* of *plugin*, or :class:`LaunchRefused`.

    Creates the plugin's data folder; nothing else is written.
    """
    if not is_plain_name(plugin):
        raise LaunchRefused("the plugin name is not a plain folder name")
    base = powers_home() if home is None else Path(home)
    record = consent.enabled() if enabled is None else enabled
    rec = apply_consent(inspect_plugin(base, plugin), record)
    if rec.state != "on":
        raise LaunchRefused(f"the plugin is {rec.state}, not on")
    entry = next((s for s in rec.servers if s.name == server), None)
    if not isinstance(entry, StdioServer):
        raise LaunchRefused("the plugin declares no valid stdio server by that name")

    root = os.path.realpath(rec.root)
    data = os.path.abspath(plugin_data_dir(base, plugin))
    try:
        os.makedirs(data, exist_ok=True)
    except OSError as exc:
        raise LaunchRefused(f"its data folder could not be created ({exc.strerror or exc})")

    env = dict(environ)
    env.update({k: expand(v, root, data) for k, v in entry.env.items()})
    env["PLUGIN_ROOT"] = root
    env["PLUGIN_DATA"] = data
    executable = _resolve_command(entry.command, root, env.get("PATH"))
    argv = [executable, *(expand(a, root, data) for a in entry.args)]
    return Launch(executable, argv, env, _resolve_cwd(entry.cwd, root, data))


def _refuse(plugin: str, server: str, reason: str) -> NoReturn:
    print(
        f"kirocrew: plugin MCP server {plugin}:{server} was not started: {reason}.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(_EXIT_REFUSED)


def _spawn_child(launch: Launch) -> NoReturn:
    """Windows: run the server as a child on this process's stdio, pass its status on."""
    proc = subprocess.Popen(
        launch.argv,
        executable=launch.executable,
        env=launch.env,
        cwd=launch.cwd,
        stdin=0,
        stdout=1,
        stderr=2,
    )
    try:
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    os._exit(rc if 0 <= rc <= 0x7FFFFFFF else 1)


def main(argv: list[str] | None = None) -> NoReturn:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        _refuse("?", "?", "the launcher needs exactly a plugin name and a server name")
    plugin, server = args
    try:
        launch = plan_launch(plugin, server, dict(os.environ))
    except LaunchRefused as exc:
        _refuse(plugin, server, str(exc))
    try:
        os.chdir(launch.cwd)
    except OSError as exc:
        _refuse(plugin, server, f"its working directory cannot be entered ({exc.strerror or exc})")
    if platform_compat.IS_WINDOWS:
        _spawn_child(launch)
    try:
        # The command is the owner-enabled plugin's own server, re-checked against
        # the owner's record just above; nothing here comes from the session.
        # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
        os.execve(launch.executable, launch.argv, launch.env)
    except OSError as exc:
        _refuse(plugin, server, f"its command could not be started ({exc.strerror or exc})")
    raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Host variables a service-started gateway does not inherit from the installer.

launchd and systemd start the gateway with :func:`common.service_environment` and
nothing else. A login-shell gateway passes the shell's AWS container credential
endpoint, AWS region and ssh-agent socket on to the MCP servers, cron scripts and
agent sessions it starts; under the service they are absent, so a server that
reaches AWS or pushes over git ssh fails there and works from a shell.

None of them is baked into the service: the unit, the plist and
``/etc/kirocrew/kirocrew.env`` are readable by every local user, and the container
authorization value is a bearer credential. ``~/.kiro/crew/.env`` is the supported
home, as for ``KIRO_API_KEY`` (:func:`common.headless_auth_warning`):
``load_credentials()`` reads every key from it into the gateway's environment at
boot. This module only names what is missing. A value is read only to test it
for blankness, and ``SSH_AUTH_SOCK``'s against the agent socket paths, and never
leaves this module.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import shlex
import textwrap
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from kiro_crew import platform_compat
from kiro_crew.config import loader
from kiro_crew.service import common, linux
from kiro_crew.service.common import Platform

#: The AWS SDKs' container credential provider and region settings, read from
#: the environment only. Order is the order the warning lists them in.
_AWS_ENV_NAMES = (
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
)

#: Checked under systemd only. The Linux agent-session spawn re-discovers a live
#: agent socket only at the per-login paths ``acp.client._resolve_ssh_auth_sock``
#: globs, so a socket at a fixed path elsewhere is lost. On macOS that spawn finds
#: launchd's agent socket itself, and its path changes at each boot, so a copy in
#: ``.env`` would only go stale.
_SYSTEMD_ONLY_NAMES = ("SSH_AUTH_SOCK",)

#: The Linux paths ``acp.client._resolve_ssh_auth_sock`` globs, in its order
#: (pinned equal by test). A socket at one of them is found again by every agent
#: session, and a per-login one is gone at logout, so a copy in ``.env`` would
#: only go stale: it is not named.
_AGENT_SOCKET_GLOBS = (
    "/tmp/ssh-*/agent.*",
    "/run/user/{uid}/ssh-agent.socket",
    "/run/user/{uid}/keyring/ssh",
)

#: A name left out when the name it maps to is also set. The SDKs read the
#: authorization file in preference to the inline value, so carrying the file's
#: path is enough and the inline bearer value need not be copied anywhere.
_SUPERSEDED_BY = {
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
}


def _agent_socket_globs() -> "tuple[str, ...]":
    """:data:`_AGENT_SOCKET_GLOBS` for this process's uid (``-1`` where there is none)."""
    uid = platform_compat.effective_uid()
    return tuple(pattern.format(uid=-1 if uid is None else uid) for pattern in _AGENT_SOCKET_GLOBS)


def _found_again(sock: str) -> bool:
    """Whether *sock* is a path :data:`_AGENT_SOCKET_GLOBS` matches, one segment at a time.

    Segment by segment, as ``glob`` matches: ``fnmatch``'s ``*`` alone would also
    cross a ``/``, and the session spawn would not find a socket that deep. The
    paths are Linux paths, so they are normalised as POSIX ones on every host.
    """
    parts = PurePosixPath(posixpath.normpath(sock.strip())).parts
    for pattern in _agent_socket_globs():
        segments = PurePosixPath(pattern).parts
        if len(segments) == len(parts) and all(
            fnmatch.fnmatchcase(part, segment) for part, segment in zip(parts, segments)
        ):
            return True
    return False


def _overrides_file_names() -> "set[str]":
    """The names ``/etc/kirocrew/kirocrew.env`` sets, which the unit reads too.

    Both systemd scopes load it through ``EnvironmentFile=``, so a name an
    operator set there does reach the service. A file this account cannot
    read, or one systemd could not parse (UTF-16 or UTF-32), supplies nothing.
    """
    try:
        return common._names_defined_in_env_file(linux.ENV_FILE_PATH)
    except loader.EnvFileWideEncodingError:
        return set()


def _dropped(
    environ: "Mapping[str, str] | None", plat: "Platform | None"
) -> "tuple[list[str], str]":
    """The dropped names, and the re-save line when ``.env`` cannot be decoded."""
    source = os.environ if environ is None else environ
    if plat is None:
        plat = common.current_platform()
    candidates: tuple[str, ...] = _AWS_ENV_NAMES
    if plat is Platform.SYSTEMD:
        candidates += _SYSTEMD_ONLY_NAMES
    present = [name for name in candidates if source.get(name, "").strip()]
    if "SSH_AUTH_SOCK" in present and _found_again(source["SSH_AUTH_SOCK"]):
        present.remove("SSH_AUTH_SOCK")
    if not present:
        return [], ""
    present = [name for name in present if _SUPERSEDED_BY.get(name, "") not in present]
    # Only the keys matter, and they do not depend on the home passed in.
    baked = common.service_environment(str(Path.home()))
    dotenv = common.service_dotenv(environ, plat)[0]
    reread = ""
    try:
        supplied = common._names_defined_in_env_file(dotenv)
    except loader.EnvFileWideEncodingError as exc:
        # The gateway reads a wide file as empty, so it supplies nothing.
        supplied = set()
        reread = loader.undecodable_env_message(dotenv, exc)
    if plat is Platform.SYSTEMD:
        supplied |= _overrides_file_names()
    dropped = [name for name in present if name not in baked and name not in supplied]
    return dropped, reread


def dropped_host_env_names(
    environ: "Mapping[str, str] | None" = None, plat: "Platform | None" = None
) -> "list[str]":
    """Return the host variable names the installed service will not have.

    A name counts when *environ* (``os.environ`` by default) gives it a non-blank
    value, :func:`common.service_environment` does not carry it, and neither
    ``.env`` nor, under systemd, ``/etc/kirocrew/kirocrew.env`` assigns it a
    non-blank value. *plat* (the current platform by default) decides whether
    ``SSH_AUTH_SOCK`` is checked; a socket at a path the agent-session spawn
    searches is not named. Names only, in a fixed order.
    """
    return _dropped(environ, plat)[0]


def dropped_host_env_warning(
    environ: "Mapping[str, str] | None" = None, plat: "Platform | None" = None
) -> str:
    """Return a note naming the dropped host variables, or ``""`` when none is.

    Built only from the names, the ``.env`` path and the restart command, so no
    value can reach the text. The remedy appends ``NAME=$NAME`` lines for the
    operator's shell to expand, after creating the file ``0600``.
    """
    names, reread = _dropped(environ, plat)
    if not names:
        return ""
    dotenv, account = common.service_dotenv(environ, plat)
    lines = [
        "Note: these variables are set in this shell, but the service will not",
        "   inherit them:",
        "",
        # Names hold no spaces, so a wrap never splits one.
        *(f"     {row}" for row in textwrap.wrap(" ".join(names), 68)),
        "",
        "   MCP servers, cron scripts and agent sessions that reach AWS or use git",
        "   over ssh may fail under the service without them. To carry them over,",
        f"   add them to {dotenv} (0600) and restart the service:",
    ]
    if reread:
        lines += ["", f"     First: {reread}"]
    # The append must never create the file: under a 022 umask it would be 0644
    # until load_credentials() next tightens it. The path is quoted because the
    # lines are pasted verbatim, and each value is left for the shell to expand.
    # The echo ends a last line written without a newline, which the first
    # appended assignment would otherwise join; a blank line is skipped on read.
    # One && chain, so nothing is appended when the chmod fails.
    target = shlex.quote(str(dotenv))
    assignments = " ".join(f'"{name}=${name}"' for name in names)
    lines += [
        "",
        f"     touch {target} && chmod 600 {target} && echo >> {target} &&",
        f"       printf '%s\\n' {assignments} >> {target}",
        f"     {common.restart_command_hint()}",
        "",
        "   A copied value does not follow later changes on this host, so copy only",
        "   values that stay the same while the service runs.",
    ]
    return common._with_crew_home_caveat(lines, environ, account)


def _env_file_path_value(path: Path, *, systemd: bool = False) -> str:
    """The non-blank ``PATH`` a ``.env``-style file assigns, or ``""``.

    *systemd* strips one pair of matching outer quotes, as systemd's
    ``EnvironmentFile=`` does; the data-home ``.env`` is read literally.
    """
    try:
        raw = loader.read_env_text(path, encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    value = loader.parse_env_lines(raw).get("PATH", "")
    if systemd and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _home_expanded(entry: str, home: str) -> str:
    """*entry* with a leading ``~`` expanded against *home*, not this process's."""
    if entry == "~" or entry.startswith("~/"):
        return home + entry[1:]
    return entry


def _configured_launcher_dirs(dotenv: Path, account: str, home: str) -> "list[str]":
    """The configured MCP launcher dirs of the account the service runs as.

    Under ``sudo`` on systemd that account's own ``config.json``, overlaid by its
    ``config.local.json`` (beside its ``.env``), is read with ``~`` taken as
    *home*, since this process's config and home are the elevated account's.
    """
    from kiro_crew import env as crew_env

    if not account:
        from kiro_crew.config import KiroCrewConfig

        KiroCrewConfig.load()  # publishes mcp.extra_path_dirs
        return crew_env._extra_mcp_path_dirs()
    values: object = None
    for name in ("config.json", "config.local.json"):
        try:
            raw = json.loads((dotenv.parent / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mcp = raw.get("mcp") if isinstance(raw, dict) else None
        if isinstance(mcp, dict) and "extra_path_dirs" in mcp:
            values = mcp["extra_path_dirs"]
    if isinstance(values, list):
        values = [_home_expanded(v, home) if isinstance(v, str) else v for v in values]
    return [
        *crew_env._validated_bin_dirs(values, "mcp.extra_path_dirs"),
        *crew_env._registered_path_dirs,
    ]


def missing_launcher_dirs_warning(
    environ: "Mapping[str, str] | None" = None, plat: "Platform | None" = None
) -> str:
    """Name the configured MCP launcher dirs missing from the service PATH, or ``""``.

    The configured dirs are ``mcp.extra_path_dirs`` and any a build registered,
    those that exist. They are deliberately not baked: agent sessions and the
    MCP gateway add them per spawn, but every other process the service starts
    searches only the PATH it starts with. That PATH is the baked one, or under
    systemd a ``PATH=`` in ``/etc/kirocrew/kirocrew.env``, plus the dirs a
    ``.env`` ``PATH=`` line adds at boot, so the note goes once either is fixed.
    The printed ``PATH=`` line keeps what an existing ``.env`` line adds, since
    the last assignment wins.
    """
    from kiro_crew import env as crew_env

    if plat is None:
        plat = common.current_platform()
    dotenv, account = common.service_dotenv(environ, plat)
    home = linux._home_for_user(account) if account else str(Path.home())
    configured = [d for d in _configured_launcher_dirs(dotenv, account, home) if os.path.isdir(d)]
    if not configured:
        return ""
    base = common.service_path(home)
    if plat is Platform.SYSTEMD:
        base = _env_file_path_value(linux.ENV_FILE_PATH, systemd=True) or base
    # The printed line keeps each existing entry as written (``~`` included),
    # and the comparison expands it against the service account's home.
    written = [e for e in _env_file_path_value(dotenv).split(":") if e]
    existing = crew_env._validated_bin_dirs([_home_expanded(e, home) for e in written], "PATH")
    reached = {*base.split(":"), *existing}
    missing = [d for d in dict.fromkeys(configured) if d not in reached]
    if not missing:
        return ""
    line = ":".join(dict.fromkeys([*written, *missing]))
    action = "change its PATH= line to" if existing else "add a line"
    return common._with_crew_home_caveat(
        [
            "Note: these MCP launcher directories are not on the service PATH:",
            "",
            *(f"     {d}" for d in missing),
            "",
            "   The service starts with this PATH:",
            f"     {base}",
            "",
            "   Agent sessions and the MCP gateway add them, but other processes the",
            "   service starts will not find their commands. Re-run",
            f"   `kirocrew service install` from a login shell, or in {dotenv}",
            f"   {action}",
            f"     PATH={line}",
            "   and restart the service.",
        ],
        environ,
        account,
    )

"""The fixed launcher KAS starts in place of an MCP server whose env stayed local.

``python -P -m kiro_crew.kas_env_shim --session S --agent A --server N --version V``

The gateway's projection (:mod:`kiro_crew.acp.kas_env_launch`) wrote one launch
snapshot for that session's server and gave the engine this command instead of the
server's own. This module turns the reference back into the launch: it derives the
snapshot's file name from the reference (a caller never names a path), opens it
without following a link, refuses one that is not owner-only, checks that it names
the same session, agent, server and version, and execs the recorded command with
the recorded env values added. Any mismatch, or a snapshot it cannot read, ends the
launch with one line on stderr that names the reason; the server is never started
with a partial environment.

A snapshot that records a ``url`` instead of a command is a remote server whose
``headers`` stayed local. The launcher then does not exec anything: it becomes
the stdio server itself and relays each message to that url with the recorded
headers added (:mod:`kiro_crew.kas_http_bridge`).

Standard library only and at the top of the package: the engine starts this once
per server, so it must not import the ACP stack. The bridge path adds only the
package's trust bootstrap (:mod:`kiro_crew._ssl_compat`). It prints no env value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from typing import Any, NoReturn

#: The crew data-home leaf the snapshots live in.
SNAPSHOT_LEAF = "kas-mcp-launch"
#: The module KAS runs (``python -m``).
SHIM_MODULE = "kiro_crew.kas_env_shim"
#: The key and value that mark a file as a launch snapshot of this format.
SNAPSHOT_MARKER = "kirocrew_kas_env_launch"
SNAPSHOT_FORMAT = 1
#: The env key the projection uses to tell the launcher where the data home is.
_HOME_ENV = "KIROCREW_HOME"
#: ``EX_CONFIG``: the launch's configuration is unusable.
_EXIT_REFUSED = 78
_MAX_SNAPSHOT_BYTES = 1 << 20


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


def snapshot_name(session_key: str, agent: str, server: str, version: str) -> str:
    """The snapshot file name for one write of one session's server.

    *version* is random per write, so two projections never share a file, even
    for the same session: each one owns, and removes, only what it wrote. Every
    part is hashed, so no reference can name a path outside the leaf.
    """
    return (
        "-".join(
            (
                _digest("session", session_key),
                _digest("agent", agent),
                _digest("server", server),
                _digest("version", version),
            )
        )
        + ".json"
    )


def _refuse(server: str, reason: str) -> NoReturn:
    print(
        f"kirocrew: MCP server {server!r} was not started: {reason}. Start a new session "
        "or resume this one so Kiro Crew writes its launch again.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(_EXIT_REFUSED)


def _read_snapshot(path: str, server: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        if stat.S_ISLNK(os.lstat(os.path.dirname(path)).st_mode):
            _refuse(server, "its launch directory is a link")
        fd = os.open(path, flags)
    except FileNotFoundError:
        _refuse(
            server,
            "its launch snapshot is missing (the session ended, or Kiro Crew restarted)",
        )
    except OSError as exc:
        _refuse(server, f"its launch snapshot cannot be opened ({exc.strerror or exc})")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse(server, "its launch snapshot is not a regular file")
        if os.name == "posix":
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                _refuse(server, "its launch snapshot is not owner-only")
        if info.st_size > _MAX_SNAPSHOT_BYTES:
            _refuse(server, "its launch snapshot is too large")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(_MAX_SNAPSHOT_BYTES + 1)
    finally:
        if fd != -1:
            os.close(fd)
    try:
        doc = json.loads(raw)
    except ValueError:
        _refuse(server, "its launch snapshot is not valid JSON")
    if not isinstance(doc, dict):
        _refuse(server, "its launch snapshot has the wrong shape")
    return doc


def resolve_launch(argv: list[str], environ: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The verified ``(argv, env)`` to exec for this reference, or a refusal."""
    server, doc, environ = _resolve_snapshot(argv, environ)
    return _stdio_launch(server, doc, environ)


def resolve_bridge(server: str, doc: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """The verified ``(url, headers)`` of a remote server's snapshot, or a refusal."""
    url, headers = doc.get("url"), doc.get("headers")
    if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
        _refuse(server, "its launch snapshot names no http(s) url")
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        _refuse(server, "its launch snapshot has malformed headers")
    return url, headers


def _resolve_snapshot(
    argv: list[str], environ: dict[str, str]
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """``(server, snapshot, environ)`` once the snapshot matches the reference."""
    parser = argparse.ArgumentParser(prog=SHIM_MODULE, add_help=False)
    for flag in ("--session", "--agent", "--server", "--version"):
        parser.add_argument(flag, required=True)
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        _refuse("?", "the launcher reference is malformed")
    server = ns.server
    home = environ.pop(_HOME_ENV, "")
    if not home or not os.path.isabs(home):
        _refuse(server, "the launcher was not told where Kiro Crew's data home is")
    path = os.path.join(
        home, SNAPSHOT_LEAF, snapshot_name(ns.session, ns.agent, server, ns.version)
    )
    doc = _read_snapshot(path, server)
    if doc.get(SNAPSHOT_MARKER) != SNAPSHOT_FORMAT:
        _refuse(server, "its launch snapshot has an unknown format")
    for key in ("session", "agent", "server", "version"):
        if doc.get(key) != getattr(ns, key):
            _refuse(server, f"its launch snapshot belongs to a different {key}")
    return server, doc, environ


def _stdio_launch(
    server: str, doc: dict[str, Any], environ: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    command, args, env = doc.get("command"), doc.get("args"), doc.get("env")
    if not isinstance(command, str) or not command.strip():
        _refuse(server, "its launch snapshot names no command")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        _refuse(server, "its launch snapshot has malformed arguments")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        _refuse(server, "its launch snapshot has a malformed environment")
    return [command, *args], {**environ, **env}


def _spawn_child(argv: list[str], env: dict[str, str]) -> NoReturn:
    """Windows has no in-place exec: run the server as a child on this process's stdio.

    The engine waits on this process's pid and talks over its pipes, so the
    launcher stays alive as the server's parent and exits with its status, the
    shape ``mcp_gateway.stub_fallback._fallback_spawn_child`` uses.
    """
    resolved = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    proc = subprocess.Popen([resolved, *argv[1:]], env=env, stdin=0, stdout=1, stderr=2)
    try:
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    os._exit(rc if 0 <= rc <= 0x7FFFFFFF else 1)


def main(argv: list[str] | None = None) -> NoReturn:
    server, doc, environ = _resolve_snapshot(
        list(sys.argv[1:] if argv is None else argv), dict(os.environ)
    )
    if "url" in doc:
        # A remote server whose headers stayed local: this process becomes the
        # stdio server the engine talks to, and adds them to every request.
        # Platform trust comes first, as in every other Crew entry point, so a
        # bundled interpreter verifies an https url against the system's CAs.
        from kiro_crew._ssl_compat import _ensure_ssl_certs
        from kiro_crew.kas_http_bridge import run_bridge

        url, headers = resolve_bridge(server, doc)
        _ensure_ssl_certs()
        raise SystemExit(run_bridge(url, headers, server=server))
    launch, env = _stdio_launch(server, doc, environ)
    if os.name == "nt":
        _spawn_child(launch, env)
    try:
        # The recorded command is the operator's own server from their agent file,
        # read back from the snapshot the gateway wrote; nothing here comes from a peer.
        # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
        os.execvpe(launch[0], launch, env)
    except OSError as exc:
        print(
            f"kirocrew: MCP server command {launch[0]!r} could not be started: "
            f"{exc.strerror or exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(127) from None
    raise SystemExit(1)


if __name__ == "__main__":
    main()

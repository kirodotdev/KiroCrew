"""Ask the local Tailscale daemon a read-only question, LocalAPI first, CLI second.

Two questions are asked: ``status`` (this machine's own tailnet addresses and
login) and ``whois`` of a peer ``ip:port``. The answers decide who may open the
dashboard over the direct tailnet listener, so the transport is held to the
same planted-binary rule :mod:`kiro_crew.dashboard.tailnet` applies to the CLI:
nothing is reached through a location the gateway user can write.

Order, per platform:

* Linux: the daemon's unix socket (``/var/run/tailscale/tailscaled.sock`` or
  ``/run/tailscale/tailscaled.sock``), then the CLI.
* macOS: the open-source daemon's unix socket (``/var/run/tailscaled.socket``),
  then the standalone app's LocalAPI port and token (``/Library/Tailscale``),
  then the CLI found on ``PATH``, then the CLI inside the app bundle.
* Windows: the CLI from ``C:\\Program Files\\Tailscale``.

The CLI is accepted only from a location :func:`tailnet._trusted_candidate`
vets. A ``PATH`` hit is used only when it resolves to one of the vetted
install locations, so an agent that can write into a ``PATH`` directory cannot
plant a ``tailscale`` that answers ``whois`` with a name of its choosing.

Nothing here raises. Every failure is an :class:`Answer` with ``data=None``,
which every caller treats as "not verified".
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from kiro_crew.platform_compat import IS_MACOS, IS_WINDOWS
from kiro_crew.sandbox import scrub_env
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: Hard ceiling on one daemon round trip.
TIMEOUT_SECS = 3.0
#: Largest LocalAPI or CLI answer read. A status of a large tailnet with peers
#: omitted is a few kilobytes; whois is smaller still.
MAX_ANSWER_BYTES = 2 * 1024 * 1024

#: The ``Host`` the daemon's LocalAPI requires.
_LOCALAPI_HOST = "local-tailscaled.sock"

#: POSIX-only daemon sockets; ``_unix_socket_paths`` offers none on Windows.
LINUX_SOCKETS = (
    str(PurePosixPath("/", "var", "run", "tailscale", "tailscaled.sock")),
    str(PurePosixPath("/", "run", "tailscale", "tailscaled.sock")),
)
MACOS_SOCKETS = (str(PurePosixPath("/", "var", "run", "tailscaled.socket")),)
#: The standalone macOS app publishes its LocalAPI port as a symlink target here
#: and the matching token in ``sameuserproof-<port>``. Root-owned.
MACOS_MACSYS_DIR = "/Library/Tailscale"
MACOS_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

METHOD_LOCALAPI_UNIX = "localapi_unix"
METHOD_LOCALAPI_MACSYS = "localapi_macsys"
METHOD_CLI_PATH = "cli_path"
METHOD_CLI_APP_BUNDLE = "cli_app_bundle"
METHOD_CLI = "cli"
METHOD_NONE = "none"
METHODS = (
    METHOD_LOCALAPI_UNIX,
    METHOD_LOCALAPI_MACSYS,
    METHOD_CLI_PATH,
    METHOD_CLI_APP_BUNDLE,
    METHOD_CLI,
    METHOD_NONE,
)


@dataclass(frozen=True)
class Answer:
    """One daemon answer: parsed JSON or ``None``, how it was asked, and whether a retry may help."""

    data: Any
    method: str
    transient: bool = False


def _root_owned_and_sealed(path: str) -> bool:
    """Whether *path* and its directory are owned by root and not writable by others.

    The LocalAPI is an identity oracle here, so a socket or token file someone
    else could replace is not one to trust.
    """
    try:
        st_file = os.lstat(path)
        st_dir = os.stat(os.path.dirname(path) or "/")
    except OSError:
        return False
    if stat.S_ISLNK(st_file.st_mode):
        return False
    if st_file.st_uid != 0 or st_dir.st_uid != 0:
        return False
    return not (st_dir.st_mode & 0o022)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection over a unix socket, for the daemon's LocalAPI."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__(_LOCALAPI_HOST, timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined,unused-ignore]
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._unix_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _http_get(
    conn: http.client.HTTPConnection, path: str, headers: dict[str, str]
) -> Answer | None:
    """GET *path* on *conn*; ``None`` when the transport itself failed."""
    try:
        conn.request("GET", path, headers={"Host": _LOCALAPI_HOST, **headers})
        resp = conn.getresponse()
        body = resp.read(MAX_ANSWER_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        logger.debug("tailscale LocalAPI %s failed: %s", path, exc)
        return None
    finally:
        conn.close()
    if len(body) > MAX_ANSWER_BYTES:
        return Answer(None, METHOD_NONE)
    if resp.status != 200:
        logger.debug("tailscale LocalAPI %s answered %d", path, resp.status)
        return Answer(None, METHOD_NONE)
    try:
        return Answer(json.loads(body.decode("utf-8")), METHOD_NONE)
    except ValueError:
        return Answer(None, METHOD_NONE)


def _unix_socket_paths() -> tuple[str, ...]:
    if IS_WINDOWS:
        return ()
    if IS_MACOS:
        return MACOS_SOCKETS
    return LINUX_SOCKETS


def _localapi_unix(path: str) -> Answer | None:
    for sock_path in _unix_socket_paths():
        if not os.path.exists(sock_path):
            continue
        if not _root_owned_and_sealed(sock_path):
            logger.debug("tailscale LocalAPI socket %s is not root-owned; not used", sock_path)
            continue
        answer = _http_get(_UnixHTTPConnection(sock_path, TIMEOUT_SECS), path, {})
        if answer is not None and answer.data is not None:
            return Answer(answer.data, METHOD_LOCALAPI_UNIX)
    return None


def _macsys_port_and_token() -> tuple[int, str] | None:
    """The standalone macOS app's LocalAPI port and token, or ``None``."""
    link = os.path.join(MACOS_MACSYS_DIR, "ipnport")
    try:
        if os.lstat(link).st_uid != 0:
            return None
        port_text = os.readlink(link)
    except OSError:
        return None
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not 0 < port < 65536:
        return None
    proof = os.path.join(MACOS_MACSYS_DIR, f"sameuserproof-{port}")
    if not _root_owned_and_sealed(proof):
        return None
    try:
        with open(proof, encoding="utf-8") as fh:
            token = fh.read(512).strip()
    except OSError:
        return None
    return (port, token) if token else None


def _localapi_macsys(path: str) -> Answer | None:
    if not IS_MACOS:
        return None
    found = _macsys_port_and_token()
    if found is None:
        return None
    port, token = found
    auth = base64.b64encode(f":{token}".encode()).decode("ascii")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT_SECS)
    answer = _http_get(conn, path, {"Authorization": f"Basic {auth}"})
    if answer is not None and answer.data is not None:
        return Answer(answer.data, METHOD_LOCALAPI_MACSYS)
    return None


def cli_candidates() -> list[tuple[str, str]]:
    """``(path, method)`` for each CLI this host may run, in the order tried."""
    from kiro_crew.dashboard import tailnet

    out: list[tuple[str, str]] = []
    vetted = {os.path.realpath(p) for p in tailnet._CLI_CANDIDATE_PATHS}
    on_path = shutil.which("tailscale")
    if on_path and os.path.realpath(on_path) in vetted:
        trusted = tailnet._trusted_candidate(on_path)
        if trusted:
            out.append((trusted, METHOD_CLI_PATH))
    for candidate in tailnet._CLI_CANDIDATE_PATHS:
        if not tailnet._candidate_present(candidate):
            continue
        trusted = tailnet._trusted_candidate(candidate)
        if not trusted or any(trusted == seen for seen, _ in out):
            continue
        method = METHOD_CLI_APP_BUNDLE if candidate == MACOS_APP_CLI else METHOD_CLI
        out.append((trusted, method))
    return out


def _run_cli(cli: str, args: list[str]) -> tuple[Any | None, bool]:
    try:
        proc = subprocess.run(  # noqa: S603 - vetted absolute binary, fixed argv, no shell
            [cli, *args],
            capture_output=True,
            **UTF8_TEXT,
            timeout=TIMEOUT_SECS,
            check=False,
            env=scrub_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tailscale %s failed to run: %s", args[0], exc)
        return None, True
    if proc.returncode != 0:
        return None, False
    out = proc.stdout or ""
    if len(out) > MAX_ANSWER_BYTES:
        return None, False
    try:
        return json.loads(out), False
    except ValueError:
        return None, False


def _ask(localapi_path: str, cli_args: list[str]) -> Answer:
    for transport in (_localapi_unix, _localapi_macsys):
        try:
            answer = transport(localapi_path)
        except Exception:  # noqa: BLE001 - a transport that cannot run answers nothing
            logger.debug("tailscale LocalAPI transport failed", exc_info=True)
            answer = None
        if answer is not None:
            return answer
    transient = False
    for cli, method in cli_candidates():
        data, was_transient = _run_cli(cli, cli_args)
        if data is not None:
            return Answer(data, method)
        transient = transient or was_transient
    return Answer(None, METHOD_NONE, transient)


def whois(addr: str) -> Answer:
    """``whois`` of *addr* (``ip:port`` or ``ip``) from the daemon."""
    return _ask(f"/localapi/v0/whois?addr={quote(addr, safe='')}", ["whois", "--json", addr])


def status() -> Answer:
    """This node's status without the peer list."""
    return _ask("/localapi/v0/status?peers=false", ["status", "--json", "--peers=false"])


__all__ = [
    "Answer",
    "METHODS",
    "cli_candidates",
    "status",
    "whois",
]

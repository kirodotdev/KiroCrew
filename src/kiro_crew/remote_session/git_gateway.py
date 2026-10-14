"""Gateway side of the remote git proxy: the credential stays on this machine.

A remote session's git runs in its workspace, but its credential never goes
there. ``git_proxy.py`` in the guest frames each git HTTP request over the
session's authenticated ``wire`` link to :class:`GitProxyChannel` here, which:

1. accepts only git's smart-HTTP endpoints (``info/refs`` for the two git
   services, ``git-upload-pack``, ``git-receive-pack``) on the hosts in
   :data:`HOSTS`, each with its own repository-path shape, and rebuilds the
   upstream URL from the validated parts, never from the request, so the guest
   cannot aim it anywhere else;
2. asks governance whether this session may reach that host
   (``network.egress``, fail-closed, audited);
3. reads the ref-update commands that open each push and refuses the whole
   push when any of them updates or deletes a protected branch, using the
   same branch list and ref normalisation as the local git-publish check
   (:func:`is_protected_ref`); every other push goes through unprompted, as a
   local session's would, and a body that cannot be read is refused;
4. takes the credential from that host's sources on this machine at that
   moment (``gh``, ``glab``, ``az``, or git's own credential helper), adds it to
   the upstream request only, and streams the response back. Under the
   ``strict`` agent sandbox tier it lends none: reads go upstream anonymously
   and pushes are refused (:func:`gateway_logins_lent`).

A credential lives only in this process's memory, briefly cached, and is never
logged, framed or written anywhere. Push authentication failures include a
sign-in hint; reads may retry anonymously and preserve the upstream status.
The command environment disables interactive Git prompts.

Git LFS's batch and lock-verification POSTs also inherit Git's URL rewrite.
They receive a 307 to the original HTTPS endpoint and run directly from the
workspace, without a gateway credential. Public LFS works anonymously; private
LFS requires the workspace's own LFS authentication. Object transfers never
pass through this credentialed smart-HTTP channel.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional

from kiro_crew.remote_session.link import RemoteLink

logger = logging.getLogger(__name__)

SERVICES = ("git-upload-pack", "git-receive-pack")
_LFS_OPS = ("info/lfs/objects/batch", "info/lfs/locks/verify")
_OPS = r"(?P<op>info/refs|git-upload-pack|git-receive-pack|" + "|".join(_LFS_OPS) + ")"
_SEG = r"[A-Za-z0-9_.-]{1,255}"
#: Azure DevOps names may hold spaces and other characters, percent-encoded.
_AZ_SEG = r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}){1,255}"
#: Separator of an HTTPS URL path (RFC 3986), not a filesystem one: the same on
#: every OS, so pathlib/os.path never split it.
_URL_PATH_SEPARATOR = "/"


@dataclass(frozen=True)
class GitHost:
    """One proxied git host: what a repository path looks like, and its credentials."""

    host: str
    #: Matches ``<repo path>/<op>`` after ``/<host>/``; group ``repo`` is the path.
    pattern: "re.Pattern[str]"
    #: Credential sources tried in order (see :data:`_SOURCES`).
    sources: tuple[str, ...]
    #: Upstream wants ``.git`` on the path (GitLab redirects otherwise).
    force_dot_git: bool = False


HOSTS: dict[str, GitHost] = {
    h.host: h
    for h in (
        GitHost(
            "github.com",
            re.compile(rf"(?P<repo>[A-Za-z0-9-]{{1,39}}/{_SEG})/{_OPS}"),
            ("gh",),
        ),
        GitHost(
            "gitlab.com",
            # Nested groups: group/subgroup/.../project.
            re.compile(rf"(?P<repo>(?:{_SEG}/){{1,20}}{_SEG})/{_OPS}"),
            ("glab", "git-credential"),
            force_dot_git=True,
        ),
        GitHost(
            "bitbucket.org",
            re.compile(rf"(?P<repo>{_SEG}/{_SEG})/{_OPS}"),
            ("git-credential",),
        ),
        GitHost(
            "dev.azure.com",
            # ORG/PROJECT/_git/REPO, or ORG/_git/REPO when they share a name.
            re.compile(rf"(?P<repo>[A-Za-z0-9-]{{1,50}}/(?:{_AZ_SEG}/)?_git/{_AZ_SEG})/{_OPS}"),
            ("az", "git-credential"),
        ),
    )
}
#: Request headers passed upstream (already filtered in the guest; re-checked here).
_UP_HEADERS = ("content-type", "accept", "content-encoding", "git-protocol", "user-agent")
#: Response headers passed back to git. Lengths and transfer codings are not:
#: the guest re-frames the body.
_DOWN_HEADERS = ("content-type", "content-encoding", "cache-control", "expires", "pragma")
_CHUNK = 24 * 1024
TOKEN_TTL_SECS = 300.0
#: How often the guest is told which hosts the gateway can lend a login for.
LEND_REFRESH_SECS = 60.0
_GH_TIMEOUT_SECS = 15.0
_UPSTREAM_TIMEOUT_SECS = 30 * 60.0
MAX_LIVE_REQUESTS = 16
#: Body frames the guest may send for one request before this side takes one
#: and grants a ``credit`` back (``git_proxy.UPLOAD_WINDOW``, which ships with
#: this module). Each request's queue holds that many plus its end marker, so
#: ``serve`` never waits on a queue and reads every request's frames, a
#: ``cancel`` included, while another request's upload is stalled upstream.
UPLOAD_WINDOW = 64
_MAX_FETCH_BODY = 8 * 1024 * 1024
#: Most bytes of a push read before its ref-update commands end. A command is
#: about 100 bytes, so this holds tens of thousands of refs.
_MAX_PUSH_COMMANDS = 8 * 1024 * 1024
#: pkt-line length prefix: four hex digits (git reads either case).
_PKT_LEN = re.compile(rb"[0-9a-fA-F]{4}")
#: Largest pkt-line git writes (LARGE_PACKET_MAX).
_PKT_MAX = 65520
_OID = rb"(?:[0-9a-f]{40}|[0-9a-f]{64})"
#: ``<old> <new> <ref>``; git sends the full ref name, and a ref holds no
#: control character or space.
_PUSH_COMMAND = re.compile(
    rb"(?P<old>" + _OID + rb") (?P<new>" + _OID + rb") (?P<ref>refs/[^\x00-\x20\x7f]+)"
)
_SHALLOW = re.compile(rb"shallow " + _OID)


class GitProxyRefusal(Exception):
    """A request the gateway will not carry; ``status`` and text go back to git."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class GitRequest:
    method: str
    host: str
    repo_path: str
    op: str
    service: str

    @property
    def repository(self) -> str:
        return f"{self.host}/{self.repo_path.removesuffix('.git')}"

    @property
    def is_push(self) -> bool:
        return self.service == "git-receive-pack"

    @property
    def is_lfs(self) -> bool:
        return self.op in _LFS_OPS

    def upstream_url(self) -> str:
        path = self.repo_path
        if HOSTS[self.host].force_dot_git and not path.endswith(".git"):
            path += ".git"
        url = f"https://{self.host}/{path}/{self.op}"
        return f"{url}?service={self.service}" if self.op == "info/refs" else url


def _segments_are_safe(repo_path: str) -> bool:
    from urllib.parse import unquote

    for segment in repo_path.split(_URL_PATH_SEPARATOR):
        plain = unquote(segment)
        if (
            plain in ("", ".", "..", "-")
            or plain.startswith("-")
            or any(c in plain for c in "/\\?#@:")
            or any(ord(c) < 0x20 for c in plain)
        ):
            return False
    return True


def parse_request(method: str, path: str) -> GitRequest:
    """Validate one guest request into a :class:`GitRequest`, or refuse it."""
    raw_path, _, query = path.partition("?")
    host_name, _, rest = raw_path.lstrip("/").partition("/")
    host = HOSTS.get(host_name) if raw_path.startswith("/") else None
    match = host.pattern.fullmatch(rest) if host is not None else None
    if host is None or match is None:
        served = ", ".join(sorted(HOSTS))
        raise GitProxyRefusal(403, f"Kiro Crew: only git over https to {served} is proxied")
    repo_path, op = match["repo"], match["op"]
    if not _segments_are_safe(repo_path):
        raise GitProxyRefusal(403, "Kiro Crew: that repository name is not allowed")
    if op == "info/refs":
        if method != "GET" or query not in {f"service={s}" for s in SERVICES}:
            raise GitProxyRefusal(403, "Kiro Crew: only git's own ref discovery is proxied")
        service = query.split("=", 1)[1]
    else:
        if method != "POST" or query:
            raise GitProxyRefusal(403, "Kiro Crew: only git's own pack exchange is proxied")
        service = op
    return GitRequest(method, host.host, repo_path, op, service)


def is_protected_ref(ref: str) -> bool:
    """True when ``ref``, a destination ref a push sends, is a protected branch.

    The same branch list and normalisation as the local git-publish check
    (which reads the command lower-cased): ``refs/heads/main``,
    ``refs/remotes/<r>/main`` and any case of them all name ``main``. Parity is
    at the level of the destination refs git sends: the local check also
    refuses command SPELLINGS it cannot resolve (a bare push, ``--all``, a
    wildcard), which git has already resolved to concrete refs by the time a
    push reaches the proxy, so those are judged here by the refs they became.
    """
    from kiro_crew.security import _PROTECTED_BRANCHES, _normalize_ref

    return _normalize_ref(ref.lower()) in _PROTECTED_BRANCHES


def _unreadable_push() -> GitProxyRefusal:
    return GitProxyRefusal(
        403, "Kiro Crew: the push's ref updates could not be read, so it was refused"
    )


def _pkt_line(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


class PushCommands:
    """Incremental reader of the ref-update commands that open a push body.

    ``git-receive-pack`` request bodies start with optional ``shallow`` lines,
    then one pkt-line per command (``<old> <new> <ref>``, capabilities after a
    NUL on the first), then a flush; the pack follows. :meth:`feed` returns True
    once the flush is read. Anything else, a signed push's certificate
    included, raises a refusal: what cannot be read is not carried.
    """

    def __init__(self) -> None:
        self.refs: list[str] = []
        self.capabilities: frozenset[str] = frozenset()
        self.buffered = bytearray()
        self._pos = 0
        self._done = False

    def feed(self, data: bytes) -> bool:
        self.buffered += data
        if len(self.buffered) > _MAX_PUSH_COMMANDS and not self._done:
            raise GitProxyRefusal(413, "Kiro Crew: the push's ref updates are too large")
        while not self._done:
            head = bytes(self.buffered[self._pos : self._pos + 4])
            if len(head) < 4:
                return False
            if not _PKT_LEN.fullmatch(head):
                raise _unreadable_push()
            size = int(head, 16)
            if size == 0:
                self._pos += 4
                self._done = True
                break
            if size < 5 or size > _PKT_MAX:
                raise _unreadable_push()
            if len(self.buffered) - self._pos < size:
                return False
            self._line(bytes(self.buffered[self._pos + 4 : self._pos + size]))
            self._pos += size
        return True

    def _line(self, payload: bytes) -> None:
        payload = payload.removesuffix(b"\n")
        if not self.refs and _SHALLOW.fullmatch(payload):
            return
        command, nul, caps = payload.partition(b"\0")
        if nul and self.refs:  # capabilities ride on the first command only
            raise _unreadable_push()
        if command.startswith(b"push-cert"):
            raise GitProxyRefusal(
                403, "Kiro Crew: signed pushes are not carried through the gateway"
            )
        match = _PUSH_COMMAND.fullmatch(command)
        if match is None:
            raise _unreadable_push()
        try:
            self.refs.append(match["ref"].decode("utf-8"))
        except UnicodeDecodeError:
            raise _unreadable_push() from None
        if nul:
            self.capabilities = frozenset(caps.decode("ascii", "replace").split())

    def rejection(self, refused: "Iterable[str]", message: str) -> Optional[bytes]:
        """A receive-pack result refusing every command, as a server hook would.

        git then prints ``! [remote rejected] <ref> (<reason>)`` per ref and
        ``message`` as a ``remote:`` line, where a bare HTTP error would show
        only ``RPC failed; HTTP 403``. None when the client asked for no report.
        """
        if not self.capabilities & {"report-status", "report-status-v2"}:
            return None
        # A set: tens of thousands of refs fit under the command cap, and a
        # list lookup per ref made the report quadratic on the event loop.
        protected = frozenset(refused)
        lines = [b"unpack ok\n"]
        for ref in self.refs:
            reason = (
                "protected branch"
                if ref in protected
                else "refused with the protected branch in the same push"
            )
            lines.append(f"ng {ref} {reason}\n".encode("utf-8"))
        if any(len(line) + 4 > _PKT_MAX for line in lines):
            return None  # a ref too long to report on; the caller answers 403
        report = b"".join(_pkt_line(line) for line in lines) + b"0000"
        bands = self.capabilities & {"side-band-64k", "side-band"}
        if not bands:
            return report
        # A band packet carries its 4-byte length and 1-byte band in its limit.
        room = (_PKT_MAX if "side-band-64k" in bands else 1000) - 5
        # Joined once: a report for tens of thousands of refs split into small
        # side-band packets would be quadratic as repeated bytes concatenation.
        packets = [
            _pkt_line(band + data[start : start + room])
            for band, data in ((b"\x02", message.encode("utf-8") + b"\n"), (b"\x01", report))
            for start in range(0, len(data), room)
        ]
        return b"".join(packets) + b"0000"


# ── Credentials on the gateway machine ───────────────────────────────────

#: Azure DevOps' Entra resource id: a token for it authorizes Azure Repos.
_AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


class _NoCredential(Exception):
    """This source has nothing for the host; the next source is tried.

    ``hint`` is set when the reason is one the person can act on that the
    host's general sign-in hint does not cover.
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _path_scoped_only(host: str, keys: Iterable[str]) -> str:
    """A hint when every helper set for ``host`` is scoped to a path under it.

    The lookup asks per host, with no repository path, so a helper set for one
    organisation (``credential.https://dev.azure.com/<org>.helper``) is not
    matched; it is reported rather than read as "no login".
    """
    prefix = f"credential.https://{host}/"
    keys = [k.lower() for k in keys]
    scoped = [k for k in keys if k.startswith(prefix) and len(k) > len(prefix) + len(".helper")]
    general = {"credential.helper", f"credential.https://{host}.helper", f"{prefix}.helper"}
    if not scoped or general & set(keys):
        return ""
    return (
        f"git's credential helper is set for a path under https://{host} only "
        f"({scoped[0]}), which is not read yet: set it for https://{host} "
        f"(credential.https://{host}.helper) instead"
    )


def _binary(name: str) -> Optional[str]:
    """Only protected provider installs may receive the gateway's credentials."""
    from kiro_crew.github_runner import (
        PROVIDER_CLI_OVERRIDE_ENV,
        provider_executable_candidates,
        validate_provider_executable,
    )

    override = os.environ.get(PROVIDER_CLI_OVERRIDE_ENV[name])
    candidates = (override,) if override is not None else provider_executable_candidates(name)
    for candidate in candidates:
        try:
            return validate_provider_executable(candidate, require_protected=True)
        except ValueError:
            continue
    return None


def _credential_env(provider: str = "git") -> dict[str, str]:
    """Keep host login locations, without executable or interpreter injection."""
    from kiro_crew.apps.registry import minimal_env
    from kiro_crew.github_runner import gh_env
    from kiro_crew.platform_compat import trusted_system_path

    env = gh_env() if provider in ("git", "gh") else minimal_env()
    keys = {
        "glab": ("GLAB_CONFIG_DIR", "GITLAB_TOKEN"),
        "az": ("AZURE_CONFIG_DIR",),
    }.get(provider, ())
    env.update({key: os.environ[key] for key in keys if key in os.environ})
    env["PATH"] = trusted_system_path() or os.defpath
    for key in tuple(env):
        if key.upper().startswith(("GIT_", "PYTHON", "NODE_", "LD_", "DYLD_")):
            del env[key]
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no",
        PYTHONNOUSERSITE="1",
    )
    return env


# Only GET runs: store/cache read their fixed default location, never persist a token.
# Not dev_fleet's _sanitize_helper_value: that pins helpers git later runs with
# `store` (so it must exclude store/cache) and leaves name lookup to git; this
# one only ever runs `get` and resolves each helper to a validated absolute binary.
_CREDENTIAL_HELPERS = frozenset(
    {"store", "cache", "osxkeychain", "manager", "manager-core", "libsecret", "wincred"}
)


def _helper_value(value: str, exec_path: str, git: str = "") -> Optional[str]:
    """Synthesize a fixed command, never execute a configured shell program."""
    import shlex

    argv = _helper_argv(value, exec_path, git)
    if argv is None:
        return None
    return "!" + shlex.join(argv) if argv else ""


def _helper_argv(value: str, exec_path: str, git: str = "") -> Optional[list[str]]:
    """The validated argv a configured helper selects; ``[]`` for git's list reset.

    None when the value names nothing trusted: a configured shell program, an
    unknown helper, or a binary that fails the protected-install walk. ``git``
    (the already-validated git binary) is used only on Windows, where helpers
    run directly: Git Credential Manager ships beside it, and git's builtin
    helpers (``store``, ``cache``) have no dashed executable there, so they run
    as ``git credential-<name>``.
    """
    import shlex

    from kiro_crew.github_runner import validate_provider_executable

    if not value:
        return []  # Git's explicit helper-list reset.
    if value.startswith("!"):
        try:
            parts = shlex.split(value[1:])
        except ValueError:
            return None
        if len(parts) != 3 or parts[1:] != ["auth", "git-credential"]:
            return None
        if os.path.basename(parts[0]) not in ("gh", "gh.exe"):
            return None
        binary = _binary("gh")
        return [binary, "auth", "git-credential"] if binary else None
    if value not in _CREDENTIAL_HELPERS:
        return None
    # Git's install supplies these helpers, or a trusted system directory does
    # (Git Credential Manager installs to /usr/local/bin, outside the exec-path).
    # Even a trusted Git can report an unprotected exec-path; validate the
    # selected helper's entire hierarchy wherever it was found.
    from kiro_crew.platform_compat import trusted_system_path

    windows = os.name == "nt"
    suffix = ".exe" if windows else ""
    name = f"git-credential-{value}{suffix}"
    system_dirs = [d for d in (trusted_system_path() or "").split(os.pathsep) if d]
    if windows and git:
        # Git for Windows ships Git Credential Manager beside git.exe
        # (mingw64\\bin), outside its exec-path; the same walk vouches for it.
        system_dirs.insert(0, os.path.dirname(git))
    candidates = [os.path.join(exec_path, name)]
    if windows and git:
        # Run directly, a missing exec-path entry is skipped here rather than
        # failing to start (git's builtins have no dashed executable there).
        candidates = [c for c in candidates if os.path.isfile(c)]
    candidates += [
        os.path.join(d, name) for d in system_dirs if os.path.isfile(os.path.join(d, name))
    ]
    for candidate in candidates:
        try:
            binary = validate_provider_executable(candidate, require_protected=True)
        except ValueError:
            continue
        return [binary]
    if windows and git and value in ("store", "cache"):
        return [git, f"credential-{value}"]
    return None


async def _kill_child(process: asyncio.subprocess.Process) -> None:
    """Stop a credential CLI that overran or whose lookup was cancelled.

    On Windows the whole tree goes (``taskkill /T``): ``az`` is ``az.cmd``, whose
    Python child would otherwise outlive ``cmd.exe`` holding the stdout pipe.
    Elsewhere the child is killed as before.
    """
    if _kills_process_tree():
        from kiro_crew.platform_compat import kill_process_tree_async

        with contextlib.suppress(OSError, ValueError):
            await kill_process_tree_async(process.pid)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def _kills_process_tree() -> bool:
    """Whether a stopped CLI's whole process tree is killed (Windows)."""
    return os.name == "nt"


async def _run_quiet(
    argv: list[str], stdin: bytes = b"", env: Optional[dict] = None, *, cwd: Optional[str] = None
) -> bytes:
    """Run a credential CLI; stdout only (it carries the secret), never logged."""
    if env is None:
        env = await asyncio.to_thread(_credential_env)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=cwd or os.path.abspath(os.sep),
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(stdin), _GH_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        await _kill_child(process)
        await process.wait()
        raise _NoCredential(f"{os.path.basename(argv[0])} timed out") from None
    except asyncio.CancelledError:
        # A torn-down session cancels a lookup mid-run: the child must not
        # outlive it. Shielded so a second cancel cannot skip the reap.
        await asyncio.shield(_kill_child(process))
        await asyncio.shield(process.wait())
        raise
    if process.returncode:
        raise _NoCredential(f"{os.path.basename(argv[0])} has no login")
    return out


def _single_token(out: bytes, tool: str) -> str:
    token = out.decode("ascii", "replace").strip()
    if not token or any(c.isspace() for c in token):
        raise _NoCredential(f"{tool} has no login")
    return token


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")


async def _from_gh(host: str) -> str:
    gh = await asyncio.to_thread(_binary, "gh")
    if gh is None:
        raise _NoCredential("trusted gh is not installed")
    token = _single_token(await _run_quiet([gh, "auth", "token", "--hostname", host]), "gh")
    return _basic("x-access-token", token)


async def _from_glab(host: str) -> str:
    glab = await asyncio.to_thread(_binary, "glab")
    if glab is None:
        raise _NoCredential("trusted glab is not installed")
    out = await _run_quiet(
        [glab, "config", "get", "token", "--host", host],
        env=await asyncio.to_thread(_credential_env, "glab"),
    )
    return _basic("oauth2", _single_token(out, "glab"))


async def _from_az(host: str) -> str:
    az = await asyncio.to_thread(_binary, "az")
    if az is None:
        raise _NoCredential("trusted az is not installed")
    out = await _run_quiet(
        [
            az,
            "account",
            "get-access-token",
            "--resource",
            _AZURE_DEVOPS_RESOURCE,
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ],
        env=await asyncio.to_thread(_credential_env, "az"),
    )
    return "Bearer " + _single_token(out, "az")


#: Why a Windows gateway that cannot verify its own account lends no git login.
#: Every lend path refuses before any credential source runs.
WINDOWS_LENDING_UNVERIFIED = (
    "Kiro Crew: this Windows gateway cannot verify its own user account or file "
    "permissions, so it lends no git login to remote sessions; sign in to git inside "
    "the workspace instead"
)


def lending_unverifiable() -> bool:
    """True on a Windows gateway that cannot check what it would lend through.

    A login is lent only through binaries the ACL walk of
    ``validate_provider_executable`` vouches for, and that walk is keyed on the
    gateway user's SID. When the process token's SID cannot be read, or the
    security-descriptor API cannot be loaded, nothing could be vouched for, so
    no login is lent at all (fails closed). Always False off Windows.
    """
    if os.name != "nt":
        return False
    from pathlib import Path

    from kiro_crew import platform_compat, windows_acl

    if not platform_compat.current_user_sid():
        return True
    try:
        windows_acl.describe(Path(sys.executable))
    except Exception:  # noqa: BLE001 -- unreadable descriptors: nothing can be vouched for
        return True
    return False


async def lending_unverifiable_off_loop() -> bool:
    """:func:`lending_unverifiable` for a coroutine: its Windows walk runs off the loop.

    The walk reads a security descriptor and looks up account names, which on
    a domain-joined host with no reachable domain controller can take as long
    as the lookup does; the event loop never waits on it. Off Windows the
    answer needs no system call, so it is read inline without a thread hop.
    """
    if os.name != "nt":
        return lending_unverifiable()
    return await asyncio.to_thread(lending_unverifiable)


def _runs_helpers_directly() -> bool:
    """Whether git's credential helpers are run here rather than by ``git credential fill``.

    Git runs every helper through ``sh -c``, and Git for Windows keeps ``sh``
    in its own ``usr\\bin``, which only its ``cmd\\git.exe`` launcher puts on
    ``PATH``; the trusted ``PATH`` these lookups run with has no ``sh``. So on
    Windows each validated helper executable is run directly instead.
    """
    return os.name == "nt"


async def _from_git_credential(host: str) -> str:
    """Read configured helper selectors; execute only synthesized trusted helpers."""
    from kiro_crew.github_runner import validate_provider_executable
    from kiro_crew.platform_compat import trusted_git_bin

    if await lending_unverifiable_off_loop():
        # Before git runs at all: no configuration is read and no helper starts.
        raise _NoCredential("lending is unverifiable here", hint=WINDOWS_LENDING_UNVERIFIED)
    git = await asyncio.to_thread(trusted_git_bin)
    if git is None:
        raise _NoCredential("trusted git is not installed")
    try:
        git = await asyncio.to_thread(validate_provider_executable, git, require_protected=True)
    except ValueError:
        raise _NoCredential("trusted git is not installed") from None
    cwd = os.path.dirname(git)
    env = await asyncio.to_thread(_credential_env)
    env["GIT_CEILING_DIRECTORIES"] = cwd
    # Config is data here: this query never runs a helper. NUL separates entries
    # so a newline inside an untrusted value cannot invent a configuration key.
    configured = await _run_quiet(
        [git, "config", "--null", "--get-regexp", r"^credential(\..+)?\.helper$"],
        env=env,
        cwd=cwd,
    )
    exec_path = os.fsdecode((await _run_quiet([git, "--exec-path"], env=env, cwd=cwd)).strip())
    entries: list[tuple[str, str]] = []
    direct: list[tuple[str, list[str]]] = []
    for entry in configured.decode("utf-8", "replace").split("\0"):
        key, separator, value = entry.partition("\n")
        if not separator or re.fullmatch(r"credential(?:\..+)?\.helper", key) is None:
            continue
        # A subsection is free text (``[credential "<anything>"]``), so a key can
        # hold ``=``: spelled into ``-c key=value``, git would split it at that
        # ``=`` and run the rest as the helper. Keys go to git through
        # GIT_CONFIG_KEY_n, which takes them whole, and a key holding ``=`` or a
        # control character is skipped outright.
        if "=" in key or any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
            continue
        if _runs_helpers_directly():
            argv = await asyncio.to_thread(_helper_argv, value, exec_path, git)
            if argv is not None:
                direct.append((key, argv))
            continue
        helper = await asyncio.to_thread(_helper_value, value, exec_path)
        if helper is not None:
            entries.append((key, helper))
    if _runs_helpers_directly():
        return await _fill_directly(host, direct, env)
    if not entries:
        raise _NoCredential("git has no trusted credential helpers")
    # Do not re-read agent-writable configuration after validation. Only the
    # synthesized helper entries reach fill, including host scoping and resets.
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull)
    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for index, (key, helper) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = helper
    hint = _path_scoped_only(host, (key for key, _ in entries))
    try:
        out = await _run_quiet(
            [git, "credential", "fill"],
            f"protocol=https\nhost={host}\n\n".encode(),
            env,
            cwd=cwd,
        )
    except _NoCredential as exc:
        raise _NoCredential(str(exc), hint=hint) from None
    fields = dict(
        line.split("=", 1) for line in out.decode("utf-8", "replace").splitlines() if "=" in line
    )
    user, secret = fields.get("username", ""), fields.get("password", "")
    if not user or not secret:
        raise _NoCredential("git's credential helper has nothing stored", hint=hint)
    return _basic(user, secret)


def _helper_applies(key: str, host: str) -> bool:
    """Whether git would ask the helper set under ``key`` for ``https://<host>``.

    ``credential.helper`` always applies. A URL-scoped key applies when its URL
    is ``https`` with no user name, names ``host`` (a ``*`` matches one label,
    as in git), keeps the default port and has no path: the lookup asks per
    host with no repository path, as ``git credential fill`` is asked. Any
    other spelling is not matched, so fewer helpers run, never more.
    """
    from urllib.parse import urlsplit

    if key == "credential.helper":
        return True
    url = key[len("credential.") : -len(".helper")]
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443) or parts.path not in ("", "/") or parts.query or parts.fragment:
        return False
    pattern = (parts.hostname or "").split(".")
    labels = host.lower().split(".")
    if len(pattern) != len(labels):
        return False
    return all(p == "*" or p == label for p, label in zip(pattern, labels))


def _windows_helper_order(entries: list[tuple[str, list[str]]], host: str) -> list[list[str]]:
    """The helpers git would run for ``host``, in git's order.

    Config order, each applicable entry appended; an empty value (``[]``)
    clears the list so far, as git's own reset does.
    """
    order: list[list[str]] = []
    for key, argv in entries:
        if not _helper_applies(key, host):
            continue
        if not argv:
            order.clear()
            continue
        order.append(argv)
    return order


async def _fill_directly(host: str, entries: list[tuple[str, list[str]]], env: dict) -> str:
    """``git credential fill`` without git's shell: each helper run as an argv.

    The same protocol git speaks to a helper: the request on stdin, ``key=value``
    lines back on stdout. As in git, a helper that fails or answers nothing is
    passed over, a user name one helper gives is sent to the next, a password
    whose ``password_expiry_utc`` has passed is dropped, and ``quit`` stops the
    lookup. The secret stays in this process: it is read from the helper's
    stdout pipe and sent to no later helper.
    """
    helpers = _windows_helper_order(entries, host)
    hint = _path_scoped_only(host, (key for key, _ in entries))
    if not helpers:
        raise _NoCredential("git has no trusted credential helpers", hint=hint)
    user = ""
    for argv in helpers:
        request = f"protocol=https\nhost={host}\n" + (f"username={user}\n" if user else "") + "\n"
        try:
            out = await _run_quiet(
                [*argv, "get"], request.encode(), env, cwd=os.path.dirname(argv[0])
            )
        except (_NoCredential, OSError):
            # As in git: a helper that cannot start or answers nothing is passed over.
            continue
        fields = dict(
            line.split("=", 1)
            for line in out.decode("utf-8", "replace").splitlines()
            if "=" in line
        )
        user = fields.get("username", user)
        secret = fields.get("password", "")
        expiry = fields.get("password_expiry_utc", "")
        if secret and expiry.isdigit() and int(expiry) < time.time():
            secret = ""
        if user and secret:
            return _basic(user, secret)
        if fields.get("quit", "").lower() in ("1", "true", "yes", "on"):
            break
    raise _NoCredential("git's credential helper has nothing stored", hint=hint)


_SOURCES: dict[str, Callable[[str], Awaitable[str]]] = {
    "gh": _from_gh,
    "glab": _from_glab,
    "az": _from_az,
    "git-credential": _from_git_credential,
}
#: What to tell the person when no source answered, per host.
_SIGN_IN_HINT = {
    "github.com": "run gh auth login on the gateway machine",
    "gitlab.com": (
        "run glab auth login on the gateway machine, or store a token in its git "
        "credential helper"
    ),
    "bitbucket.org": (
        "store a Bitbucket app password or API token in the gateway machine's git "
        "credential helper"
    ),
    "dev.azure.com": (
        "run az login on the gateway machine, or store a PAT in its git credential helper"
    ),
}


#: Status-only commands per CLI source: exit 0 means signed in. Their output is
#: discarded unread, so a readiness check never handles a credential.
_STATUS_ARGV: dict[str, Callable[[str, str], list[str]]] = {
    "gh": lambda binary, host: [binary, "auth", "status", "--hostname", host],
    "glab": lambda binary, host: [binary, "auth", "status", "--hostname", host],
    "az": lambda binary, host: [binary, "account", "show", "--output", "none"],
}


async def login_status(host: str) -> str:
    """``signed_in`` / ``not_signed_in`` / ``unchecked`` for ``host``'s CLI login.

    Only the CLI sources are asked, by exit status. A host whose sources are
    all exhausted reports ``not_signed_in``; one that falls back to git's
    credential helper reports ``unchecked`` there, because asking the helper
    would hand this process the secret itself.
    """
    spec = HOSTS[host]
    for name in spec.sources:
        build = _STATUS_ARGV.get(name)
        binary = await asyncio.to_thread(_binary, name) if build is not None else None
        if build is None or binary is None:
            continue
        env = await asyncio.to_thread(_credential_env, name)
        process = await asyncio.create_subprocess_exec(
            *build(binary, host),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=os.path.abspath(os.sep),
            start_new_session=True,
        )
        try:
            code = await asyncio.wait_for(process.wait(), _GH_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            await _kill_child(process)
            await process.wait()
            continue
        if code == 0:
            return "signed_in"
    return "unchecked" if "git-credential" in spec.sources else "not_signed_in"


async def host_credential(host: str) -> str:
    """The ``Authorization`` value for ``host`` from this machine's own logins."""
    if await lending_unverifiable_off_loop():
        raise GitProxyRefusal(502, WINDOWS_LENDING_UNVERIFIED)
    spec = HOSTS[host]
    hints = []
    for name in spec.sources:
        try:
            return await _SOURCES[name](host)
        except _NoCredential as exc:
            if exc.hint:
                hints.append(exc.hint)
            continue
    hint = "; ".join([*hints, _SIGN_IN_HINT[host]])
    raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found; {hint}")


def _egress_permitted(session_key: str, host: str, agent: str, *, app: str = "") -> bool:
    from kiro_crew.platform.governance_profiles import vet_and_audit

    if not isinstance(agent, str) or not agent.strip():
        return False
    decision = vet_and_audit(
        "network.egress",
        host,
        session_key=session_key,
        agent=agent,
        # The owning app's profile binds first, as for the session's tool calls.
        app=app if isinstance(app, str) else "",
        tool_name="git_proxy",
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


def _configured_tier() -> Optional[str]:
    """The operator's ``agent.sandbox`` tier, or None when this load cannot tell.

    One ``KiroCrewConfig.load()`` answers both questions, so the tier and the
    check that it is real come from the same snapshot: a save landing between
    two loads cannot pair a readable first read with a defaulted second one.
    The loader does not raise on an unparseable ``config.json`` or
    ``config.local.json``, or on an ``agent`` section that is not an object: it
    stands field defaults in and marks the read degraded. The default tier is
    not ``strict``, so a degraded read is unknown, never a tier.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    cfg = KiroCrewConfig.load()
    degraded = set(getattr(cfg, "degraded_sections", ()))
    if (
        "agent" in degraded
        or any(s.startswith(DEGRADED_WHOLE_CONFIG) for s in degraded)
        or bool(getattr(cfg, "_base_unreadable", False))
        or bool(getattr(cfg, "_overlay_unreadable", False))
    ):
        return None
    tier = getattr(getattr(cfg, "agent", None), "sandbox", None)
    return tier if isinstance(tier, str) and tier else None


def gateway_logins_lent() -> bool:
    """False when the agent sandbox tier is ``strict``: no gateway login is lent.

    ``strict`` hides the gateway's git logins (``~/.config/gh``,
    ``~/.git-credentials``, ``~/.azure``) from local agents, so the proxy does
    not hand the same logins to a remote session. The tier is the configured
    one with the governance floor applied, as spawns read it. A tier that
    cannot be known (an unreadable or degraded config, or a read that fails)
    lends nothing.
    """
    from kiro_crew.sandbox import effective_sandbox_mode

    if lending_unverifiable():
        logger.info("git proxy: this Windows gateway cannot verify its account; lending no login")
        return False
    try:
        tier = _configured_tier()
        if tier is None:
            logger.warning("git proxy: agent settings unreadable; lending no gateway login")
            return False
        return effective_sandbox_mode(tier) != "strict"
    except Exception:  # noqa: BLE001 -- fail closed: lending a login needs a known tier
        logger.warning("git proxy: sandbox tier unreadable; lending no gateway login")
        return False


def _strict_refusal(what: str, *, unverifiable: bool) -> str:
    """Why a login was not lent: the unverifiable-Windows refusal there, else the strict tier's.

    ``unverifiable`` is :func:`lending_unverifiable_off_loop`'s answer, read by
    the caller, so building the message never blocks the loop.
    """
    if unverifiable:
        return f"{WINDOWS_LENDING_UNVERIFIED}, so {what} was refused"
    return (
        f"Kiro Crew: the strict agent sandbox lends no gateway git login, so {what} "
        "was refused; private repository reads and pushes are not available under "
        "the strict sandbox"
    )


async def git_readiness() -> dict[str, str]:
    """Per proxied host: ``login_status``, ``withheld`` for all under ``strict``, or
    ``unsupported`` for all on a Windows gateway that cannot verify its own account
    (no source is asked)."""
    if await lending_unverifiable_off_loop():
        return {host: "unsupported" for host in HOSTS}
    if not await asyncio.to_thread(gateway_logins_lent):
        return {host: "withheld" for host in HOSTS}
    return dict(zip(HOSTS, await asyncio.gather(*(login_status(h) for h in HOSTS))))


class GitProxyLink(RemoteLink):
    """Bound unread git bytes before the next wire feed can acknowledge more.

    The reader pauses above twice its line limit, through the base link's own
    consumer gate, so its stall check still knows this side stopped reading
    and does not drop a link whose git reader is merely busy. A pending
    transport read adds at most one 64 KiB chunk. With no more wire feeds, the
    worker's unacknowledged-byte window fills and stops draining the guest
    process, all the way back to the HTTP upload.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stdout = asyncio.StreamReader(limit=64 * 1024)
        self.stdout.set_transport(self._consumer)  # type: ignore[arg-type]

    async def _consume(self, chunk: bytes) -> None:
        await self._consumer.open.wait()
        await super()._consume(chunk)


#: host -> the ``Authorization`` header value for it.
CredentialSource = Callable[[str], Awaitable[str]]


class GitProxyChannel:
    """Serve one guest ``git_proxy.py`` over its link until the link ends."""

    def __init__(
        self,
        link: Any,
        session_key: str,
        *,
        agent: str,
        app: str = "",
        credential_source: CredentialSource = host_credential,
        egress_permitted: Optional[Callable[[str, str, str], bool]] = None,
        logins_lent: Callable[[], bool] = gateway_logins_lent,
        http_session: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.link = link
        self.session_key = session_key
        # Captured on the gateway at launch, never read from guest frames.
        self.agent = agent
        #: The chat slot's own app (session allocation's ``owning_app``), never a
        #: guest claim: its governance profile binds this session's egress first.
        self.app = app if isinstance(app, str) else ""
        self._credential_source = credential_source
        self._egress_permitted = egress_permitted or functools.partial(
            _egress_permitted, app=self.app
        )
        self._logins_lent = logins_lent
        #: Request ids forwarded anonymously because the strict tier lends nothing.
        self._withheld: set[int] = set()
        self._http = http_session
        self._owns_http = http_session is None
        self._clock = clock
        self._write_lock = asyncio.Lock()
        self._requests: "dict[int, asyncio.Queue[Optional[bytes]]]" = {}
        #: True once the guest sent a frame: a guest that refuses to start (another
        #: build's gateway) ends the link without one.
        self.heard = False
        self._tasks: "dict[int, asyncio.Task[None]]" = {}
        #: host -> (Authorization value, read at). Memory only.
        self._credentials: dict[str, tuple[str, float]] = {}
        #: host -> a credential the host refused (401). A source that keeps
        #: handing back the same revoked token must not keep the host routed
        #: here; a different value (a fresh sign-in) lends again.
        self._refused: dict[str, str] = {}
        #: One lock per host: a slow credential source for one host never
        #: holds back another host's lookup.
        self._credential_locks: dict[str, asyncio.Lock] = {}
        #: Publishes the lendable hosts once the guest asks (a ``lend`` frame).
        self._lend_task: "Optional[asyncio.Task[None]]" = None
        #: Set when a credential stops working, so the guest hears it sooner.
        self._lend_stale = asyncio.Event()

    async def _send(self, frame: dict) -> None:
        line = (json.dumps(frame, separators=(",", ":")) + "\n").encode("ascii")
        async with self._write_lock:
            await self.link.write(line)

    async def serve(self) -> None:
        """Read the guest's frames until the link ends, then wind down."""
        try:
            while True:
                try:
                    line = await self.link.stdout.readline()
                except ValueError:
                    # A frame longer than the link's line limit: the stream
                    # cannot be resynchronised, so the channel ends here.
                    logger.warning("git proxy %s: a frame overran the line limit", self.session_key)
                    return
                if not line:
                    return
                try:
                    frame = json.loads(line)
                    rid = int(frame["id"])
                    kind = frame["t"]
                except (ValueError, KeyError, TypeError):
                    logger.warning("git proxy %s: dropped a malformed frame", self.session_key)
                    continue
                self.heard = True
                if kind == "req":
                    if (
                        rid in self._requests
                        or rid in self._tasks
                        or len(self._tasks) >= MAX_LIVE_REQUESTS
                    ):
                        await self._send(
                            {
                                "t": "err",
                                "id": rid,
                                "status": 503,
                                "message": "Kiro Crew: too many live git requests or duplicate id",
                            }
                        )
                        continue
                    body: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(UPLOAD_WINDOW + 1)
                    self._requests[rid] = body
                    task = asyncio.ensure_future(self._handle(rid, frame, body))
                    self._tasks[rid] = task
                    task.add_done_callback(functools.partial(self._retire, rid, body))
                elif kind == "lend":
                    if self._lend_task is None:
                        self._lend_task = asyncio.ensure_future(self._publish_lendable())
                elif kind == "cancel":
                    cancelled_task = self._tasks.get(rid)
                    if cancelled_task is not None:
                        cancelled_task.cancel()
                        await asyncio.gather(cancelled_task, return_exceptions=True)
                elif kind in ("body", "end"):
                    queue = self._requests.get(rid)
                    if queue is None:
                        continue
                    item = base64.b64decode(frame.get("b") or "") if kind == "body" else None
                    try:
                        # Never await here: one request's stalled upload must
                        # not stop this loop reading the others' frames.
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        await self._overran(rid, queue)
        except ConnectionError:
            return
        finally:
            # However serve() ended, close the guest's input: it then clears its
            # lend file, so git uses the workspace's own login instead of hanging.
            with contextlib.suppress(Exception):
                self.link.close_input()
            if self._lend_task is not None:
                self._lend_task.cancel()
                await asyncio.gather(self._lend_task, return_exceptions=True)
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._requests.clear()
            if self._owns_http and self._http is not None:
                await self._http.close()

    def _retire(
        self,
        rid: int,
        body: "asyncio.Queue[Optional[bytes]]",
        task: "asyncio.Task[None]",
    ) -> None:
        if self._tasks.get(rid) is task:
            self._tasks.pop(rid)
            # Cancellation can arrive before _handle enters its try/finally.
            if self._requests.get(rid) is body:
                self._requests.pop(rid)

    async def _overran(self, rid: int, body: "asyncio.Queue[Optional[bytes]]") -> None:
        """Fail a request whose guest sent body frames past its credit.

        Only that request ends: its later frames are dropped and the rest of
        the link keeps flowing. The queue stays bounded either way.
        """
        logger.warning("git proxy %s: request %s overran its upload window", self.session_key, rid)
        self._stop_reading(rid, body)
        task = self._tasks.get(rid)
        if task is not None:
            task.cancel()
        await self._send(
            {
                "t": "err",
                "id": rid,
                "status": 502,
                "message": "Kiro Crew: the git request overran its upload window",
            }
        )

    async def _take(self, rid: int, body: "asyncio.Queue[Optional[bytes]]") -> Optional[bytes]:
        """The request's next body piece (None at its end), granting the guest
        one more frame of credit for each piece taken."""
        piece = await body.get()
        if piece is not None:
            await self._send({"t": "credit", "id": rid})
        return piece

    async def lendable_hosts(
        self, on_found: "Optional[Callable[[list[str]], Awaitable[None]]]" = None
    ) -> list[str]:
        """The hosts this gateway can lend a login for right now, sorted.

        Empty under ``strict`` (or an unknown tier); otherwise each host whose
        credential sources answer with a value the host has not refused. The
        values stay here: only host names go to the guest. The hosts are looked
        up at once, each under its own lock, so a slow source for one host
        (a credential CLI running to its time limit) holds back no other;
        ``on_found`` hears the sorted list so far each time a host is added.
        """
        if not await asyncio.to_thread(self._logins_lent):
            return []
        hosts: list[str] = []

        async def check(host: str) -> None:
            try:
                value = await self._credential_now(host)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- no login for this host: it goes direct
                # A refusal's text names the step to take and carries no
                # credential; any other error is named by its type only.
                reason = str(exc) if isinstance(exc, GitProxyRefusal) else type(exc).__name__
                logger.info("git proxy: not lending a login for %s: %s", host, reason)
                return
            if self._refused.get(host) == value:
                # The same revoked login: the workspace's own applies. Not
                # cached, so the next refresh re-reads the source and a fresh
                # sign-in is lent again without waiting out the token TTL.
                self._credentials.pop(host, None)
                return
            hosts.append(host)
            if on_found is not None:
                await on_found(sorted(hosts))

        await asyncio.gather(*(check(host) for host in HOSTS))
        return sorted(hosts)

    async def _publish_lendable(self) -> None:
        """Tell the guest which hosts to route here, now and every refresh.

        The guest routes only those hosts through this proxy; every other host
        keeps the workspace's own git login, so a host the gateway cannot lend
        for (or every host, under ``strict``) works as it would without Kiro
        Crew. A failure publishes nothing new, and the guest routes nothing it
        has not been told.

        Until the first full list is out, each host is published as soon as its
        login is found, marked ``partial``: the guest routes it at once, and a
        command waiting for the first answer keeps waiting, bounded, until the
        full list says no other host is still being looked up. A refresh
        publishes only its full list, so it never briefly drops a routed host.
        """
        first = True

        async def early(hosts: list[str]) -> None:
            if first:
                await self._send({"t": "lend", "id": 0, "hosts": hosts, "partial": True})

        while True:
            self._lend_stale.clear()
            try:
                hosts = await self.lendable_hosts(on_found=early)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- fail open to the workspace's own login
                hosts = []
            first = False
            await self._send({"t": "lend", "id": 0, "hosts": hosts})
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._lend_stale.wait(), LEND_REFRESH_SECS)

    async def _credential_now(self, host: str) -> str:
        async with self._credential_locks.setdefault(host, asyncio.Lock()):
            cached = self._credentials.get(host)
            if cached is not None and self._clock() - cached[1] < TOKEN_TTL_SECS:
                return cached[0]
            value = await self._credential_source(host)
            self._credentials[host] = (value, self._clock())
            return value

    async def _admit_push(
        self, rid: int, request: GitRequest, frame: dict, body: "asyncio.Queue[Optional[bytes]]"
    ) -> Optional[bytes]:
        """Read a push's ref-update commands; refuse it if any names a protected branch.

        Returns the bytes read, which go upstream first, or None once a refusal
        has been answered. A push that updates or deletes a protected branch is
        refused whole, as the local git-publish check refuses it, and nothing
        of it is sent upstream.
        """
        encoding = next(
            (
                str(v).strip().lower()
                for k, v in (frame.get("headers") or {}).items()
                if str(k).lower() == "content-encoding"
            ),
            "",
        )
        commands = PushCommands()
        try:
            if encoding not in ("", "identity"):
                raise _unreadable_push()  # git sends push bodies uncompressed
            while True:
                piece = await self._take(rid, body)
                if piece is None:
                    raise _unreadable_push()  # ended before the command list did
                if commands.feed(piece):
                    break
        except GitProxyRefusal as refusal:
            await self._audit_push(request, "denied", "unreadable-push", refusal.message)
            raise
        protected = [ref for ref in commands.refs if is_protected_ref(ref)]
        if not protected:
            await self._audit_push(request, "allowed", "no-protected-branch", "", commands.refs)
            return bytes(commands.buffered)
        message = (
            f"Kiro Crew: the push to {request.repository} was refused because "
            f"{protected[0]} is a protected branch. Remote sessions cannot update or "
            "delete it, as local sessions cannot; push a feature branch instead."
        )
        logger.info(
            "git proxy %s: refused a push to a protected branch of %s",
            self.session_key,
            request.repository,
        )
        await self._audit_push(request, "denied", "protected-branch", message, commands.refs)
        report = commands.rejection(protected, message)
        if report is None:
            raise GitProxyRefusal(403, message)
        self._stop_reading(rid, body)
        await self._send(
            {
                "t": "resp",
                "id": rid,
                "status": 200,
                "headers": {"Content-Type": "application/x-git-receive-pack-result"},
            }
        )
        for start in range(0, len(report), _CHUNK):
            chunk = report[start : start + _CHUNK]
            await self._send({"t": "body", "id": rid, "b": base64.b64encode(chunk).decode("ascii")})
        await self._send({"t": "end", "id": rid})
        return None

    async def _audit_push(
        self,
        request: GitRequest,
        outcome: str,
        rule: str,
        reason: str,
        refs: "list[str] | tuple[str, ...]" = (),
    ) -> None:
        """Write the SEL row for one push decision, allowed or denied."""
        await self._audit("git.push", " ".join([request.repository, *refs]), outcome, rule, reason)

    async def _audit(self, scope: str, item: str, outcome: str, rule: str, reason: str) -> None:
        """Write one SEL governance-decision row for a proxy decision.

        Best-effort: a failed write is logged and never changes the answer git
        gets, which has already been decided (fail-closed refusals stay refused).
        """
        agent = self.agent if isinstance(self.agent, str) and self.agent.strip() else "kirocrew"

        def write() -> None:
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key=self.session_key,
                agent=agent,
                tool_name="git_proxy",
                scope=scope,
                item=item,
                outcome=outcome,
                rule=rule,
                layer="git_proxy",
                reason=reason,
            )

        try:
            await asyncio.to_thread(write)
        except Exception:  # noqa: BLE001 -- the audit row must never change the answer
            logger.warning("git proxy %s: audit write failed", self.session_key)

    def _stop_reading(self, rid: int, body: "asyncio.Queue[Optional[bytes]]") -> None:
        """Drop the rest of a refused request's body before answering it.

        A refusal can be answered while the guest is still sending a large
        body. Unregistered, later frames for this id are dropped instead of
        overrunning a queue nothing reads any more; the guest stops sending
        once it reads the answer's ``err``.
        """
        if self._requests.get(rid) is body:
            self._requests.pop(rid)
        while not body.empty():
            body.get_nowait()

    async def _drain(
        self, rid: int, body: "asyncio.Queue[Optional[bytes]]"
    ) -> AsyncIterator[bytes]:
        while True:
            piece = await self._take(rid, body)
            if piece is None:
                return
            yield piece

    async def _handle(self, rid: int, frame: dict, body: "asyncio.Queue[Optional[bytes]]") -> None:
        try:
            method, path = str(frame.get("method") or ""), str(frame.get("path") or "")
            try:
                request = parse_request(method, path)
            except GitProxyRefusal as refusal:
                # A host or endpoint the proxy does not carry: denied before any
                # egress check, so it gets its own audit row.
                await self._audit(
                    "git.request",
                    f"{method} {path}"[:512],
                    "denied",
                    "not-proxied",
                    refusal.message,
                )
                raise
            if not isinstance(self.agent, str) or not self.agent.strip():
                message = f"Kiro Crew: governance does not allow {request.host} here"
                await self._audit("git.request", request.repository, "denied", "no-agent", message)
                raise GitProxyRefusal(403, message)
            if not await asyncio.to_thread(
                self._egress_permitted, self.session_key, request.host, self.agent
            ):
                raise GitProxyRefusal(
                    403, f"Kiro Crew: governance does not allow {request.host} here"
                )
            if request.is_lfs:
                # LFS applies Git's insteadOf too. Return it to its direct HTTPS
                # endpoint, preserving POST with 307, without reading a gateway
                # credential or exposing one in LFS's object-action headers.
                await self._send(
                    {
                        "t": "resp",
                        "id": rid,
                        "status": 307,
                        "headers": {"Location": request.upstream_url()},
                    }
                )
                await self._send({"t": "end", "id": rid})
                return
            lent = await asyncio.to_thread(self._logins_lent)
            if request.is_push and not lent:
                # Refused before reading anything: no push can work under strict.
                unverifiable = await lending_unverifiable_off_loop()
                message = _strict_refusal(
                    f"a push to {request.repository}", unverifiable=unverifiable
                )
                rule = "windows-unverified" if unverifiable else "strict-sandbox"
                await self._audit_push(request, "denied", rule, message)
                raise GitProxyRefusal(403, message)
            head = b""
            if request.op == "git-receive-pack":
                admitted = await self._admit_push(rid, request, frame, body)
                if admitted is None:
                    return  # refused, and the refusal already answered
                head = admitted
            if not lent:
                # Reads stay anonymous, so public repositories still clone.
                credential = ""
                self._withheld.add(rid)
            else:
                try:
                    credential = await self._credential_now(request.host)
                except GitProxyRefusal as refusal:
                    if request.is_push or refusal.status != 502:
                        raise
                    credential = ""
            logger.info(
                "git proxy %s: %s %s", self.session_key, request.service, request.repository
            )
            await self._forward(rid, frame, request, credential, body, head)
        except GitProxyRefusal as refusal:
            self._stop_reading(rid, body)
            await self._send(
                {"t": "err", "id": rid, "status": refusal.status, "message": refusal.message}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- git gets a clean failure, never a hang
            logger.warning("git proxy %s: request failed: %s", self.session_key, type(exc).__name__)
            self._stop_reading(rid, body)
            with contextlib.suppress(Exception):
                await self._send(
                    {
                        "t": "err",
                        "id": rid,
                        "status": 502,
                        "message": "Kiro Crew: upstream request failed",
                    }
                )
        finally:
            self._withheld.discard(rid)
            if self._requests.get(rid) is body:
                self._requests.pop(rid)
            # Release what the retired request still buffered. Any subsequent
            # frames for this id are dropped before touching a queue.
            while not body.empty():
                body.get_nowait()

    async def _session(self) -> Any:
        if self._http is None:
            import aiohttp

            self._http = aiohttp.ClientSession(
                auto_decompress=False,  # git's own Content-Encoding passes through intact
                timeout=aiohttp.ClientTimeout(total=_UPSTREAM_TIMEOUT_SECS, sock_connect=30),
            )
        return self._http

    async def _forward(
        self,
        rid: int,
        frame: dict,
        request: GitRequest,
        credential: str,
        body: "asyncio.Queue[Optional[bytes]]",
        head: bytes = b"",
    ) -> None:
        headers = {
            k: str(v)
            for k, v in (frame.get("headers") or {}).items()
            if str(k).lower() in _UP_HEADERS
        }
        replay: Optional[bytearray] = None
        if request.method == "POST" and not request.is_push and credential:
            # Only fetch negotiation needs replay on an anonymous retry. Push
            # packs stay streamed; a fetch body cannot grow without a bound.
            replay = bytearray()
            async for piece in self._drain(rid, body):
                if len(replay) + len(piece) > _MAX_FETCH_BODY:
                    raise GitProxyRefusal(413, "Kiro Crew: fetch negotiation body is too large")
                replay.extend(piece)

        async def upload() -> AsyncIterator[bytes]:
            if replay is None:
                # A push's admitted commands (read by _admit_push) go first.
                for start in range(0, len(head), _CHUNK):
                    yield head[start : start + _CHUNK]
                async for piece in self._drain(rid, body):
                    yield piece
            else:
                for start in range(0, len(replay), _CHUNK):
                    yield bytes(replay[start : start + _CHUNK])

        session = await self._session()
        for _attempt in range(2):
            if credential:
                headers["Authorization"] = credential
            else:
                headers.pop("Authorization", None)
            data = upload() if request.method == "POST" else None
            async with session.request(
                request.method,
                request.upstream_url(),
                headers=headers,
                data=data,
                allow_redirects=False,  # a redirect could leave the host with the header
            ) as response:
                status = response.status
                if status == 401 and credential:
                    self._credentials.pop(request.host, None)
                    self._refused[request.host] = credential
                    self._lend_stale.set()  # republish: the guest stops routing it here
                    if not request.is_push:
                        credential = ""
                        continue
                    self._stop_reading(rid, body)
                    await self._send(
                        {
                            "t": "err",
                            "id": rid,
                            "status": 403,
                            "message": (
                                f"Kiro Crew: {request.host} refused the gateway's credential; "
                                f"{_SIGN_IN_HINT[request.host]}"
                            ),
                        }
                    )
                    return
                if status == 401 and rid in self._withheld:
                    # A private repository under strict: say why, not a bare 401.
                    self._stop_reading(rid, body)
                    unverifiable = await lending_unverifiable_off_loop()
                    await self._send(
                        {
                            "t": "err",
                            "id": rid,
                            "status": 403,
                            "message": _strict_refusal(
                                f"reading {request.repository}", unverifiable=unverifiable
                            ),
                        }
                    )
                    return
                if 300 <= status < 400:
                    self._stop_reading(rid, body)
                    await self._send(
                        {
                            "t": "err",
                            "id": rid,
                            "status": 502,
                            "message": f"Kiro Crew: {request.host} redirected; not followed",
                        }
                    )
                    return
                down = {k: v for k, v in response.headers.items() if k.lower() in _DOWN_HEADERS}
                await self._send({"t": "resp", "id": rid, "status": status, "headers": down})
                async for piece in response.content.iter_chunked(_CHUNK):
                    await self._send(
                        {"t": "body", "id": rid, "b": base64.b64encode(piece).decode("ascii")}
                    )
                await self._send({"t": "end", "id": rid})
                return

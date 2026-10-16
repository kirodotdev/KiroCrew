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
3. asks the person before the first push to each repository in a session (a
   dashboard approval card, the same one tool calls use), and refuses when
   nobody can be asked;
4. takes the credential from that host's sources on this machine at that
   moment (``gh``, ``glab``, ``az``, or git's own credential helper), adds it to
   the upstream request only, and streams the response back.

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
import shutil
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

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
    #: The guest URL prefixes git rewrites to the proxy for this host.
    rewrites: tuple[str, ...]
    #: Upstream wants ``.git`` on the path (GitLab redirects otherwise).
    force_dot_git: bool = False


HOSTS: dict[str, GitHost] = {
    h.host: h
    for h in (
        GitHost(
            "github.com",
            re.compile(rf"(?P<repo>[A-Za-z0-9-]{{1,39}}/{_SEG})/{_OPS}"),
            ("gh",),
            ("https://github.com/", "git@github.com:", "ssh://git@github.com/"),
        ),
        GitHost(
            "gitlab.com",
            # Nested groups: group/subgroup/.../project.
            re.compile(rf"(?P<repo>(?:{_SEG}/){{1,20}}{_SEG})/{_OPS}"),
            ("glab", "git-credential"),
            ("https://gitlab.com/", "git@gitlab.com:", "ssh://git@gitlab.com/"),
            force_dot_git=True,
        ),
        GitHost(
            "bitbucket.org",
            re.compile(rf"(?P<repo>{_SEG}/{_SEG})/{_OPS}"),
            ("git-credential",),
            ("https://bitbucket.org/", "git@bitbucket.org:", "ssh://git@bitbucket.org/"),
        ),
        GitHost(
            "dev.azure.com",
            # ORG/PROJECT/_git/REPO, or ORG/_git/REPO when they share a name.
            re.compile(rf"(?P<repo>[A-Za-z0-9-]{{1,50}}/(?:{_AZ_SEG}/)?_git/{_AZ_SEG})/{_OPS}"),
            ("az", "git-credential"),
            # Azure's SSH form (v3/ORG/PROJECT/REPO) has a different path shape,
            # so only HTTPS is rewritten.
            ("https://dev.azure.com/",),
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
_GH_TIMEOUT_SECS = 15.0
_UPSTREAM_TIMEOUT_SECS = 30 * 60.0
MAX_LIVE_REQUESTS = 16
_MAX_FETCH_BODY = 8 * 1024 * 1024
#: Repositories a person approved pushing to, per session key. Bounded.
_PUSH_APPROVED: "dict[tuple[str, str], float]" = {}
_PUSH_APPROVED_MAX = 512


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


# ── Credentials on the gateway machine ───────────────────────────────────

#: Azure DevOps' Entra resource id: a token for it authorizes Azure Repos.
_AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


class _NoCredential(Exception):
    """This source has nothing for the host; the next source is tried."""


def _binary(name: str) -> Optional[str]:
    # A launchd/systemd gateway often lacks Homebrew on PATH.
    from kiro_crew.env import augmented_path

    return shutil.which(name) or shutil.which(name, path=augmented_path())


async def _run_quiet(
    argv: list[str], stdin: bytes = b"", env: Optional[dict] = None, *, cwd: Optional[str] = None
) -> bytes:
    """Run a credential CLI; stdout only (it carries the secret), never logged."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(stdin), _GH_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise _NoCredential(f"{os.path.basename(argv[0])} timed out") from None
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
    gh = _binary("gh")
    if gh is None:
        raise _NoCredential("gh is not installed")
    token = _single_token(await _run_quiet([gh, "auth", "token", "--hostname", host]), "gh")
    return _basic("x-access-token", token)


async def _from_glab(host: str) -> str:
    glab = _binary("glab")
    if glab is None:
        raise _NoCredential("glab is not installed")
    out = await _run_quiet([glab, "config", "get", "token", "--host", host])
    return _basic("oauth2", _single_token(out, "glab"))


async def _from_az(host: str) -> str:
    az = _binary("az")
    if az is None:
        raise _NoCredential("az is not installed")
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
        ]
    )
    return "Bearer " + _single_token(out, "az")


async def _from_git_credential(host: str) -> str:
    """Ask the operator's Git helpers, never an agent-writable repository's."""
    from kiro_crew.platform_compat import trusted_git_bin

    git = await asyncio.to_thread(trusted_git_bin)
    if git is None:
        raise _NoCredential("trusted git is not installed")
    # The trusted install directory cannot contain an agent-planted .git. Stop
    # discovery there, too: a gateway's own cwd can be an agent-writable checkout.
    cwd = os.path.dirname(git)
    env = {
        # Drop repository selection, config injection and executable overrides;
        # only the operator's ordinary global/system configuration supplies helpers.
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")},
        "GIT_CEILING_DIRECTORIES": cwd,
        # Answer from what is stored; never open a prompt or a browser.
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }
    out = await _run_quiet(
        [git, "credential", "fill"], f"protocol=https\nhost={host}\n\n".encode(), env, cwd=cwd
    )
    fields = dict(
        line.split("=", 1) for line in out.decode("utf-8", "replace").splitlines() if "=" in line
    )
    user, secret = fields.get("username", ""), fields.get("password", "")
    if not user or not secret:
        raise _NoCredential("git's credential helper has nothing stored")
    return _basic(user, secret)


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
        binary = _binary(name) if build is not None else None
        if build is None or binary is None:
            continue
        process = await asyncio.create_subprocess_exec(
            *build(binary, host),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = await asyncio.wait_for(process.wait(), _GH_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            continue
        if code == 0:
            return "signed_in"
    return "unchecked" if "git-credential" in spec.sources else "not_signed_in"


async def host_credential(host: str) -> str:
    """The ``Authorization`` value for ``host`` from this machine's own logins."""
    spec = HOSTS[host]
    for name in spec.sources:
        try:
            return await _SOURCES[name](host)
        except _NoCredential:
            continue
    raise GitProxyRefusal(502, f"Kiro Crew: no {host} login found; {_SIGN_IN_HINT[host]}")


def _slot_of(session_key: str) -> str:
    return session_key.split(":", 1)[1] if session_key.startswith("dashboard:") else ""


async def dashboard_push_approval(session_key: str, repository: str) -> bool:
    """Ask the person in the dashboard; False when there is no one to ask.

    ``repository`` names its host (``gitlab.com/group/project``).
    """
    from kiro_crew.slack.handler import get_dashboard_state

    state = get_dashboard_state()
    request = getattr(state, "request_approval", None)
    if request is None:
        return False
    approval_id = f"git-push-{os.urandom(6).hex()}"
    return bool(
        await request(
            approval_id,
            "git_proxy",
            f"git push to {repository}",
            tool_input=f"Push from this session's remote workspace to {repository}",
            tool_purpose="A push publishes commits with your credential on the gateway.",
            slot=_slot_of(session_key),
        )
    )


def _egress_permitted(session_key: str, host: str, agent: str) -> bool:
    from kiro_crew.platform.governance_profiles import vet_and_audit

    if not isinstance(agent, str) or not agent.strip():
        return False
    decision = vet_and_audit(
        "network.egress",
        host,
        session_key=session_key,
        agent=agent,
        tool_name="git_proxy",
        fail_closed=True,
    )
    return bool(getattr(decision, "permitted", False))


class _ReceiveWindow(asyncio.ReadTransport):
    """StreamReader's byte watermark gates consumption of the authenticated wire."""

    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()
        self.ready.set()

    def pause_reading(self) -> None:
        self.ready.clear()

    def resume_reading(self) -> None:
        self.ready.set()


class GitProxyLink(RemoteLink):
    """Bound unread git bytes before the next wire feed can acknowledge more.

    The reader pauses above twice its line limit. A pending transport read adds
    at most one 64 KiB chunk; the transport reader has its own byte watermark.
    With no more wire feeds, the worker's unacknowledged-byte window fills and
    stops draining the guest process, all the way back to the HTTP upload.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._receive_window = _ReceiveWindow()
        self.stdout = asyncio.StreamReader(limit=64 * 1024)
        self.stdout.set_transport(self._receive_window)

    async def _consume(self, chunk: bytes) -> None:
        await self._receive_window.ready.wait()
        await super()._consume(chunk)


Approver = Callable[[str, str], Awaitable[bool]]
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
        approve: Approver = dashboard_push_approval,
        credential_source: CredentialSource = host_credential,
        egress_permitted: Callable[[str, str, str], bool] = _egress_permitted,
        http_session: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.link = link
        self.session_key = session_key
        # Captured on the gateway at launch, never read from guest frames.
        self.agent = agent
        self._approve = approve
        self._credential_source = credential_source
        self._egress_permitted = egress_permitted
        self._http = http_session
        self._owns_http = http_session is None
        self._clock = clock
        self._write_lock = asyncio.Lock()
        self._requests: "dict[int, asyncio.Queue[Optional[bytes]]]" = {}
        self._tasks: "dict[int, asyncio.Task[None]]" = {}
        #: host -> (Authorization value, read at). Memory only.
        self._credentials: dict[str, tuple[str, float]] = {}
        self._credential_lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()

    async def _send(self, frame: dict) -> None:
        line = (json.dumps(frame, separators=(",", ":")) + "\n").encode("ascii")
        async with self._write_lock:
            await self.link.write(line)

    async def serve(self) -> None:
        """Read the guest's frames until the link ends, then wind down."""
        try:
            while True:
                line = await self.link.stdout.readline()
                if not line:
                    return
                try:
                    frame = json.loads(line)
                    rid = int(frame["id"])
                    kind = frame["t"]
                except (ValueError, KeyError, TypeError):
                    logger.warning("git proxy %s: dropped a malformed frame", self.session_key)
                    continue
                if kind == "req":
                    if rid in self._requests or len(self._tasks) >= MAX_LIVE_REQUESTS:
                        await self._send(
                            {
                                "t": "err",
                                "id": rid,
                                "status": 503,
                                "message": "Kiro Crew: too many live git requests or duplicate id",
                            }
                        )
                        continue
                    body: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(64)
                    self._requests[rid] = body
                    task = asyncio.ensure_future(self._handle(rid, frame, body))
                    self._tasks[rid] = task
                    task.add_done_callback(functools.partial(self._retire, rid, body))
                elif kind == "cancel":
                    cancelled_task = self._tasks.get(rid)
                    if cancelled_task is not None:
                        cancelled_task.cancel()
                        await asyncio.gather(cancelled_task, return_exceptions=True)
                elif kind in ("body", "end"):
                    queue = self._requests.get(rid)
                    if queue is None:
                        continue
                    if kind == "body":
                        await queue.put(base64.b64decode(frame.get("b") or ""))
                    else:
                        await queue.put(None)
        except ConnectionError:
            return
        finally:
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

    async def _credential_now(self, host: str) -> str:
        async with self._credential_lock:
            cached = self._credentials.get(host)
            if cached is not None and self._clock() - cached[1] < TOKEN_TTL_SECS:
                return cached[0]
            value = await self._credential_source(host)
            self._credentials[host] = (value, self._clock())
            return value

    async def _push_allowed(self, repository: str) -> bool:
        key = (self.session_key, repository.lower())
        async with self._push_lock:  # one card per repository, not one per request
            if key in _PUSH_APPROVED:
                return True
            if not await self._approve(self.session_key, repository):
                return False
            if len(_PUSH_APPROVED) >= _PUSH_APPROVED_MAX:
                _PUSH_APPROVED.pop(next(iter(_PUSH_APPROVED)))
            _PUSH_APPROVED[key] = self._clock()
            return True

    async def _drain(self, body: "asyncio.Queue[Optional[bytes]]") -> AsyncIterator[bytes]:
        while True:
            piece = await body.get()
            if piece is None:
                return
            yield piece

    async def _handle(self, rid: int, frame: dict, body: "asyncio.Queue[Optional[bytes]]") -> None:
        try:
            request = parse_request(str(frame.get("method") or ""), str(frame.get("path") or ""))
            if (
                not isinstance(self.agent, str)
                or not self.agent.strip()
                or not await asyncio.to_thread(
                    self._egress_permitted, self.session_key, request.host, self.agent
                )
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
            if request.is_push and not await self._push_allowed(request.repository):
                raise GitProxyRefusal(
                    403, f"Kiro Crew: push to {request.repository} was not approved"
                )
            try:
                credential = await self._credential_now(request.host)
            except GitProxyRefusal as refusal:
                if request.is_push or refusal.status != 502:
                    raise
                credential = ""
            logger.info(
                "git proxy %s: %s %s", self.session_key, request.service, request.repository
            )
            await self._forward(rid, frame, request, credential, body)
        except GitProxyRefusal as refusal:
            await self._send(
                {"t": "err", "id": rid, "status": refusal.status, "message": refusal.message}
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- git gets a clean failure, never a hang
            logger.warning("git proxy %s: request failed: %s", self.session_key, type(exc).__name__)
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
            if self._requests.get(rid) is body:
                self._requests.pop(rid)
            # A pending serve() put must wake when its consumer retires. Any
            # subsequent frames for this id are dropped before touching a queue.
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
            async for piece in self._drain(body):
                if len(replay) + len(piece) > _MAX_FETCH_BODY:
                    raise GitProxyRefusal(413, "Kiro Crew: fetch negotiation body is too large")
                replay.extend(piece)

        async def upload() -> AsyncIterator[bytes]:
            if replay is None:
                async for piece in self._drain(body):
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
                    if not request.is_push:
                        credential = ""
                        continue
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
                if 300 <= status < 400:
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

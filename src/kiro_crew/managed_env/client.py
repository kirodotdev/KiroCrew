"""Client for the Kiro managed-environment service (Smithy rpc-v2-cbor).

Seven operations: ``ListSandboxes``, ``GetSandbox``, ``CreateSandbox``,
``StopSandbox``, ``ResumeSandbox``, ``DeleteSandbox`` and ``StartSshSession``.

* The endpoint comes from ``instances.managed_env_endpoint`` (:mod:`.settings`), read
  on every call; unset or malformed refuses the call by name before anything is sent.
  It is ``https://`` (``http://`` only to a loopback host), and a redirect is never
  followed.
* The credential comes from an injected callable returning a :class:`Credential`, the
  shape of ``apps/kiro_calls.SignedCall``: the bearer, the sign-in id the guard's
  backoff is keyed on, and the sign-in's profile ARN. No bearer means no request. The bearer is in no log line, no
  error and no ``repr``.
* Every id-taking call needs an id this Crew created (:class:`.guard.SandboxGuard`);
  Delete goes by that id only, never by ``displayName``.
* ``CreateSandbox`` is never retried. A lost reply is recorded with the guard before
  its error is raised, and new creates pause.
* One call at a time, :data:`CALL_TIMEOUT_S` each, at most :data:`MAX_REPLY_BYTES` read.
"""

from __future__ import annotations

import http.client
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from kiro_crew.apps.rpc_cbor import cbor_decode, cbor_encode
from kiro_crew.bounded_read import bounded_read
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.guard import GuardRefused, SandboxGuard, is_quota_refusal
from kiro_crew.managed_env.settings import LaneSettings, SettingsMissing

log = logging.getLogger(__name__)

SERVICE = "KiroWebBearerService"
IDP = "AWSIdC"
CALL_TIMEOUT_S = 30.0
MAX_REPLY_BYTES = 4 * 1024 * 1024
LIST_PAGE_SIZE = 50
LIST_MAX_PAGES = 100
POLL_INTERVAL_S = 10.0
POLL_MAX = 90
DISPLAY_NAME_MAX = 128
_MESSAGE_MAX = 200
#: 401 and 403 bodies can echo what was sent: their message is never kept.
_QUIET_STATUSES = (401, 403)
#: A CreateSandbox failure with no status that may still have made an environment.
LOST_REPLY_KINDS = ("unreachable", "bad_reply")
#: The shape of a Smithy error type name.
_KIND_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")


@dataclass(frozen=True)
class Credential:
    """The bearer for one call, the sign-in it belongs to and that sign-in's Kiro
    profile ARN (sent when set, as ``apps/kiro_calls`` does). ``repr`` hides the bearer."""

    bearer: str = field(repr=False)
    sign_in: str = ""
    profile_arn: str = ""


class SandboxError(Exception):
    """A call that did not succeed. ``kind`` is the service's error type, or one of
    ``not_configured``, ``no_token``, ``unreachable``, ``bad_reply``."""

    def __init__(self, op: str, kind: str, status: Optional[int] = None, message: str = ""):
        text = f"{op}: HTTP {status} {kind}" if status else f"{op}: {kind}"
        super().__init__(f"{text}: {message}" if message else text)
        self.op = op
        self.kind = kind
        self.status = status
        self.message = message

    @property
    def not_found(self) -> bool:
        return self.status == 404


class SandboxStateError(Exception):
    """A polled environment did not reach RUNNING. ``kind`` names why."""

    def __init__(self, kind: str, message: str, last: Optional[dict] = None):
        super().__init__(message)
        self.kind = kind
        self.last = last or {}


@dataclass(frozen=True)
class SshSession:
    """A StartSshSession reply. ``stream_url`` and ``token_value`` are secrets."""

    session_id: str
    stream_url: str = field(repr=False)
    token_value: str = field(repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3XX is never followed: the bearer goes to the configured endpoint only."""

    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None  # urllib then raises the 3XX as an HTTPError


class SandboxClient:
    """``credential()`` returns the current :class:`Credential` (raises or returns an
    empty bearer when signed out). ``settings``, ``sleep`` and ``clock`` are for tests."""

    def __init__(
        self,
        credential: Callable[[], Credential],
        guard: SandboxGuard,
        *,
        settings: Optional[Callable[[], LaneSettings]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._credential = credential
        self._guard = guard
        self._settings = settings or (lambda: lane_settings.read_settings())
        self._sleep = sleep
        self._lock = threading.Lock()

    # -- the one wire call ---------------------------------------------------------

    def call(self, op: str, body: dict, *, ignore_backoff: bool = False) -> dict:
        """POST one operation and decode its reply, or raise SandboxError / GuardRefused."""
        with self._lock:
            try:
                endpoint = self._settings().service_endpoint()
            except SettingsMissing as e:
                raise SandboxError(op, "not_configured", message=str(e)) from None
            try:
                cred = self._credential()
            except Exception:  # noqa: BLE001 -- a sign-in error can quote token bytes
                cred = Credential("")
            if not isinstance(cred.bearer, str) or not cred.bearer:
                raise SandboxError(op, "no_token", message="Sign in to Kiro first.")
            if not ignore_backoff:
                self._guard.check_backoff(cred.sign_in)
            status, reply, kind = self._post(op, body, endpoint, cred)
            self._guard.note_reply(op, status, kind, cred.sign_in)
            if status != 200:
                raise self._error(op, status, kind, reply, cred.bearer)
            return reply

    def _post(self, op: str, body: dict, endpoint: str, cred: Credential) -> tuple[int, dict, str]:
        headers = {
            "Authorization": f"Bearer {cred.bearer}",
            "X-Kiro-Idp": IDP,
            "Content-Type": "application/cbor",
            "Accept": "application/cbor",
            "smithy-protocol": "rpc-v2-cbor",
        }
        if cred.profile_arn:
            headers["X-Kiro-Profile-Arn"] = cred.profile_arn
        url = f"{endpoint}/service/{SERVICE}/operation/{op}"
        req = urllib.request.Request(url, data=cbor_encode(body), method="POST", headers=headers)
        handlers: list = [_NoRedirect()]
        if lane_settings.is_loopback_url(endpoint):
            handlers.append(urllib.request.ProxyHandler({}))
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=CALL_TIMEOUT_S) as resp:
                status, raw = resp.status, bounded_read(resp, MAX_REPLY_BYTES + 1)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = bounded_read(e, MAX_REPLY_BYTES + 1)
            except (http.client.HTTPException, OSError):
                raw = b""
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            # The class name only: a URLError's text can carry the URL.
            log.warning("%s: no reply (%s)", op, type(e).__name__)
            raise SandboxError(op, "unreachable", message=type(e).__name__) from None
        if len(raw) > MAX_REPLY_BYTES:
            raise SandboxError(op, "bad_reply", status, "the reply is too large")
        try:
            reply = cbor_decode(raw, timestamps=True) if raw else {}
        except (ValueError, UnicodeDecodeError):
            if status != 200:
                reply = {}
            else:
                raise SandboxError(op, "bad_reply", message="the reply is not CBOR") from None
        if not isinstance(reply, dict):
            if status == 200:
                raise SandboxError(op, "bad_reply", message="the reply is not a map")
            reply = {}
        kind = str(reply.get("__type", "")).rsplit("#", 1)[-1]
        # An error type is a short Smithy name; anything else (an echoed header, say) is
        # dropped, so it can never carry a credential into a log or an error.
        if not _KIND_RE.fullmatch(kind) or cred.bearer in kind:
            kind = ""
        if status != 200:
            log.warning("%s: HTTP %s %s", op, status, kind)
        return status, reply, kind

    @staticmethod
    def _error(op: str, status: int, kind: str, reply: dict, bearer: str) -> SandboxError:
        kind = kind or f"HTTP{status}"
        message = ""
        if status not in _QUIET_STATUSES:
            message = str(reply.get("message") or reply.get("Message") or "")
            # The exact bearer first (a bare token matches no secret pattern), then known
            # secrets, before the cut, as apps/kiro_calls does.
            from kiro_crew.security.text_redaction import redact_and_truncate

            message = redact_and_truncate(message.replace(bearer, "[REDACTED]"), _MESSAGE_MAX)
        return SandboxError(op, kind, status, message)

    # -- operations ----------------------------------------------------------------

    def list_sandboxes(self) -> list[dict]:
        """Every environment of the signed-in user, from every client; others are left alone."""
        body: dict = {"maxResults": LIST_PAGE_SIZE}
        entries: list[dict] = []
        for _ in range(LIST_MAX_PAGES):
            out = self.call("ListSandboxes", body)
            page = out.get("sandboxes") or []
            if not isinstance(page, list):
                raise SandboxError("ListSandboxes", "bad_reply", message="sandboxes is not a list")
            entries += [p for p in page if isinstance(p, dict)]
            token = out.get("nextToken")
            if not token:  # an empty page that still carries a token keeps paging
                return entries
            body = {"maxResults": LIST_PAGE_SIZE, "nextToken": token}
        raise SandboxError("ListSandboxes", "bad_reply", message="too many pages")

    def create_sandbox(self, display_name: str) -> dict:
        """One CreateSandbox, never retried. A lost reply pauses new creates."""
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= DISPLAY_NAME_MAX:
            raise ValueError(f"displayName must be 1-{DISPLAY_NAME_MAX} characters")
        self._guard.check_unconfirmed()
        # The ledger must be readable, and the attempt recorded, before the create: an
        # id with nowhere to go, or an attempt Crew can't remember, could orphan it.
        self._guard.ledger()
        self._guard.begin_create(display_name)
        try:
            out = self.call("CreateSandbox", {"displayName": display_name})
        except GuardRefused:
            self._guard.end_create()  # refused before anything was sent
            raise
        except SandboxError as e:
            if e.status is not None and e.status != 200:
                self._guard.end_create()  # the service answered: nothing was made
                if is_quota_refusal(e.op, e.status, e.kind):
                    e.kind = "quota"
            elif e.kind not in LOST_REPLY_KINDS:
                self._guard.end_create()  # not_configured / no_token: nothing was sent
            raise
        self._guard.record_created(out, display_name)  # keeps the attempt when it fails
        self._guard.end_create()
        return _summary("CreateSandbox", out)

    def get_sandbox(self, exec_environment_id: str) -> dict:
        self._guard.require_own(exec_environment_id)
        return _summary("GetSandbox", self.call("GetSandbox", _id(exec_environment_id)))

    def stop_sandbox(self, exec_environment_id: str) -> dict:
        self._guard.require_own(exec_environment_id)
        return _summary("StopSandbox", self.call("StopSandbox", _id(exec_environment_id)))

    def resume_sandbox(self, exec_environment_id: str) -> dict:
        self._guard.require_own(exec_environment_id)
        return _summary("ResumeSandbox", self.call("ResumeSandbox", _id(exec_environment_id)))

    def delete_sandbox(self, exec_environment_id: str, *, cleanup: bool = False) -> dict:
        """By ledger id only. A 404 means already gone. ``cleanup`` (a failed launch's own
        environment) is sent even inside the backoff: one Delete is not a burst, and an
        environment left behind would bill with no row."""
        self._guard.require_own(exec_environment_id, deleted=True)
        try:
            out = _summary(
                "DeleteSandbox",
                self.call("DeleteSandbox", _id(exec_environment_id), ignore_backoff=cleanup),
            )
        except SandboxError as e:
            if not e.not_found:
                raise
            out = {"execEnvironmentId": exec_environment_id, "status": "DELETED", "gone": True}
        self._guard.record_deleted(exec_environment_id)
        return out

    def start_ssh_session(self, exec_environment_id: str) -> SshSession:
        """A new session on every call; never resumes a stopped environment."""
        self._guard.require_own(exec_environment_id)
        body = {"identifier": {"executionEnvironmentId": exec_environment_id}}
        out = self.call("StartSshSession", body)
        values = [out.get(k) for k in ("ssmSessionId", "streamUrl", "tokenValue")]
        if not all(isinstance(v, str) and v for v in values):
            raise SandboxError("StartSshSession", "bad_reply", message="the session is incomplete")
        return SshSession(*values)  # type: ignore[arg-type]

    def wait_running(self, exec_environment_id: str) -> dict:
        """Poll GetSandbox every :data:`POLL_INTERVAL_S`, at most :data:`POLL_MAX` times.

        Ends early on ``FAILED`` or ``DELETED``, or on ``STOPPED`` after a start was seen.
        A passing outage (a 429, a 5XX, no reply, or the backoff it starts) costs one read,
        not the launch: the poll goes on while it has reads left.
        """
        got: dict = {}
        started = False
        for n in range(POLL_MAX):
            if n:
                self._sleep(POLL_INTERVAL_S)
            try:
                got = self.get_sandbox(exec_environment_id)
            except SandboxError as e:
                if e.kind == "unreachable" or e.status == 429 or (e.status or 0) >= 500:
                    continue
                raise
            except GuardRefused as e:
                if e.reason == "backoff":
                    continue
                raise
            status = got["status"]
            if status == "RUNNING":
                return got
            if status in ("FAILED", "DELETED"):
                raise SandboxStateError(status.lower(), f"the environment is {status}", got)
            if status == "STOPPED" and started:
                raise SandboxStateError("stopped", "the environment stopped while starting", got)
            started = started or status != "STOPPED"
        raise SandboxStateError("timeout", f"not RUNNING after {POLL_MAX} reads", got)


def _id(exec_environment_id: str) -> dict:
    return {"execEnvironmentId": exec_environment_id}


def _summary(op: str, out: dict) -> dict:
    if not out.get("execEnvironmentId") or not out.get("status"):
        raise SandboxError(op, "bad_reply", message="the reply has no id or status")
    return out


__all__ = [
    "Credential",
    "GuardRefused",
    "SandboxClient",
    "SandboxError",
    "SandboxStateError",
    "SshSession",
]

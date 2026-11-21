"""Client for the managed-environment API (``KiroWebBearerService``, rpcv2Cbor).

All seven operations, with fixed wire bounds:

* ``ListSandboxes`` loops pages until ``nextToken`` is absent (an empty page may
  still carry one) and hands the entries to the guard. An entry this client did not
  create is another client's environment (the web app, the IDE, another Crew),
  returned and left alone.
* ``GetSandbox`` is polled every 10 s, at most 90 times, by :meth:`BffClient.wait_running`;
  it ends early on a non-200 answer, ``FAILED``, ``DELETED`` or ``STOPPED`` after a start.
* ``CreateSandbox`` is not idempotent: no retry, ever. An attempt whose answer was lost
  is recorded, and new launches wait (``guard.before_create``). A refusal at the
  account's cap carries the List's counts (``listed``, ``listed_other``) for the
  caller's text.
* ``ResumeSandbox`` retries every 3 s for at most 60 s on a "not ready" 409: the
  service's "not in a state that allows this operation" text or its
  ``EnvironmentNotReadyException`` kind; any other error, 409 included, is final. After
  the first such 409, one ``GetSandbox``: ``FAILED``, ``DELETED`` or a 404 ends the
  Resume at once, and ``PENDING`` or ``RUNNING`` ends it with no error.
* ``DeleteSandbox``: 404 means gone.
* ``StartSshSession`` mints a new session on every call; no retry here.
* ``UpdateSandbox`` (the idle timeout, or the name) is never retried: a repeat during
  the restart it caused is a 409.

One call at a time, at least 1 s apart, 60 s call timeout. A 401 / 403 / 429 / 5XX
answer stops the client for the rest of the process, except the account's cap on
CreateSandbox, the organization's policy 403 on a per-box action, and a List outage
(``guard.note_status``). A box whose StartSshSession got that 403 is refused from memory
for ``guard.POLICY_HOLD_S`` before it is asked again; a Get, a Resume or a served session
for the box ends that, and its line, at once. Every id-taking call is refused unless the
guard knows the id.

The endpoint and the profile ARN come from ``instances.managed_env_endpoint`` and
``instances.managed_env_profile_arn``, read on every call: with either unset the call
is refused by name before anything is sent. The bearer comes from an injected
``token_provider`` callable; this module never stores, logs or puts a bearer in an
error. Stdlib only.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from kiro_crew.managed_env import cbor
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.call_log import CallLog
from kiro_crew.managed_env.guard import (
    ClientStopped,
    GuardRefused,
    SandboxGuard,
    is_policy_refusal,
    is_quota_refusal,
    is_rename_switch_unreadable,
)
from kiro_crew.managed_env.settings import LaneSettings, SettingsMissing

log = logging.getLogger(__name__)

IDP = "AWSIdC"
_SERVICE_PATH = "/service/KiroWebBearerService/operation/"
#: The write behind the product's own Settings > Network screen.
PORTAL_SERVICE_PATH = "/service/KiroWebPortalService/operation/"

CALL_TIMEOUT_S = 60.0
MIN_CALL_GAP_S = 1.0
LIST_PAGE_SIZE = 50
LIST_MAX_PAGES = 100
POLL_INTERVAL_S = 10.0
POLL_MAX = 90
RESUME_RETRY_INTERVAL_S = 3.0
RESUME_RETRY_WINDOW_S = 60.0
RESUME_NOT_READY_TEXT = "not in a state that allows this operation"  # the service's 409 text
RESUME_NOT_READY_KIND = "EnvironmentNotReadyException"  # the 409 kind, when nothing maps it
RESUME_END_STATUSES = ("FAILED", "DELETED")  # the one Get after a not-ready 409 ends on these
#: The one Get after a not-ready 409 that reads the box starting or up ends the Resume with no
#: error and no repeat call (an idle-timeout restart's box, say): there is nothing to resume.
RESUME_UP_STATUSES = ("PENDING", "RUNNING")
DISPLAY_NAME_MAX = 128
_MESSAGE_MAX = 200
# 401 / 403 bodies can echo what was sent: their message is never kept.
_QUIET_STATUSES = (401, 403)
#: A CreateSandbox error with no HTTP status that may still have made an environment: no
#: answer arrived, or it could not be read. (``no_token`` is raised before any request.)
UNCONFIRMED_KINDS = ("unreachable", "bad_response")


class SandboxError(Exception):
    """A non-200 answer or a transport failure. ``kind`` is the exception name after
    ``#`` in ``__type`` (or ``unreachable`` / ``bad_response`` / ``not_configured``);
    ``status`` the HTTP code."""

    def __init__(self, op: str, kind: str, status: int | None = None, message: str = ""):
        text = f"{op}: HTTP {status} {kind}" if status else f"{op}: {kind}"
        if message:
            text += f": {message}"
        super().__init__(text)
        self.op = op
        self.kind = kind
        self.status = status
        self.message = message
        #: The decoded error answer, for the call log's field names only; never shown.
        self.answer: object = None
        #: On a CreateSandbox refused at the account's cap: how many environments the List
        #: just before it showed, and how many of those this client did not create.
        self.listed: int | None = None
        self.listed_other: int | None = None

    @property
    def not_found(self) -> bool:
        return self.status == 404


class SandboxStateError(Exception):
    """A polled or retried environment did not reach the wanted state. ``kind`` names why."""

    def __init__(self, kind: str, message: str, last: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.last = last or {}


@dataclass(frozen=True)
class SshTuple:
    """The answer of StartSshSession. ``stream_url`` and ``token_value`` are secrets:
    the repr and str show neither."""

    ssm_session_id: str
    stream_url: str = field(repr=False)
    token_value: str = field(repr=False)

    def __repr__(self) -> str:
        return f"SshTuple(ssm_session_id={self.ssm_session_id!r}, stream_url=<hidden>, token_value=<hidden>)"

    __str__ = __repr__


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3XX is never followed: the bearer must go to the configured endpoint only."""

    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None  # urllib then raises the 3XX as an HTTPError


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _checked_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    scheme = urllib.parse.urlsplit(endpoint).scheme
    if scheme != "https" and not (scheme == "http" and _is_loopback(endpoint)):
        raise ValueError("endpoint must be https (http only for a loopback host)")
    return endpoint


class BffClient:
    """``token_provider()`` returns the current access token string. ``endpoint`` and
    ``profile_arn`` are for tests (a loopback fake); the product passes neither, and every
    call then reads ``instances.managed_env_*`` (``settings``). ``sleep`` and ``clock`` are
    injected by tests so nothing really waits."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        guard: SandboxGuard,
        *,
        endpoint: Optional[str] = None,
        profile_arn: Optional[str] = None,
        settings: Optional[Callable[[], LaneSettings]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        call_log: CallLog | None = None,
    ):
        self._call_log = call_log
        self._fixed_endpoint = _checked_endpoint(endpoint) if endpoint is not None else None
        self._fixed_profile = profile_arn
        self._settings = settings or (lambda: lane_settings.read_settings())
        self._token = token_provider
        self._guard = guard
        self._sleep = sleep
        self._clock = clock
        # re-entrant: exclusive() holds it across two calls (a read and its write)
        self._lock = threading.RLock()
        self._last_end: float | None = None
        self.call_seq = (
            0  # one per wire call; the network-setting step reads it to prove "no call between"
        )
        #: The sign-in whose full List fills the guard's accepted set, read in the List's own
        #: thread. ``""`` (or unset) keeps the ledger-only rule. Set by the engine.
        self.accept_fingerprint: Optional[Callable[[], str]] = None
        self.last_op = ""
        self._openers: dict[bool, urllib.request.OpenerDirector] = {}

    def exclusive(self):
        """Hold the one-call-at-a-time lock across several calls (no other thread's call can slip between)."""
        return self._lock

    def _target(self, op: str) -> tuple[str, str]:
        """(endpoint, profile ARN) for this call, or a ``not_configured`` refusal naming the keys."""
        if self._fixed_endpoint is not None:
            return self._fixed_endpoint, self._fixed_profile or ""
        s = self._settings()
        try:
            s.require_service()
            return _checked_endpoint(s.endpoint), s.profile_arn
        except (SettingsMissing, ValueError) as exc:
            raise SandboxError(op, "not_configured", message=str(exc)) from None

    def _opener(self, endpoint: str) -> urllib.request.OpenerDirector:
        loop = _is_loopback(endpoint)
        opener = self._openers.get(loop)
        if opener is None:
            handlers: list = [_NoRedirect()]
            if loop:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = self._openers[loop] = urllib.request.build_opener(*handlers)
        return opener

    # -- the one wire call ---------------------------------------------------------

    def call(
        self,
        op: str,
        body: dict,
        *,
        service: str = _SERVICE_PATH,
        ignore_stop: bool = False,
        rename: bool = False,
    ) -> dict:
        """POST one operation; decode the answer or raise :class:`SandboxError`."""
        with self._lock:  # one call at a time
            if (
                not ignore_stop
            ):  # only the network-setting UNDO sets this: it must run even after a 5XX stopped the client
                self._guard.check_not_stopped()
            endpoint, profile_arn = self._target(op)
            if self._last_end is not None:
                wait = MIN_CALL_GAP_S - (self._clock() - self._last_end)
                if wait > 0:
                    self._sleep(wait)
            self.call_seq += 1
            self.last_op = op
            try:
                out = self._post(op, body, service, endpoint, profile_arn, rename=rename)
                self._note(op, 200, "", body, out)
                return out
            except SandboxError as e:
                self._note(op, e.status, e.kind, body, e.answer)
                raise
            finally:
                self._last_end = self._clock()

    def _note(self, op: str, status: int | None, kind: str, body: dict, answer: object) -> None:
        if self._call_log is not None:
            self._call_log.record(
                op, status, kind=kind, request=body, response=answer, guard=self._guard.mode
            )

    def _post(
        self,
        op: str,
        body: dict,
        service: str,
        endpoint: str,
        profile_arn: str,
        *,
        rename: bool = False,
    ) -> dict:
        bearer = self._token()
        if not bearer or not isinstance(bearer, str):
            raise SandboxError(op, "no_token", message="no bearer token available")
        headers = {
            "Authorization": f"Bearer {bearer}",
            "X-Kiro-Idp": IDP,
            "X-Kiro-Profile-Arn": profile_arn,
            "Content-Type": "application/cbor",
            "Accept": "application/cbor",
            "smithy-protocol": "rpc-v2-cbor",
        }
        req = urllib.request.Request(
            endpoint + service + op, data=cbor.dumps(body), method="POST", headers=headers
        )
        try:
            with self._opener(endpoint).open(req, timeout=CALL_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise self._http_error(op, e, bearer, rename=rename) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            # A lost answer: none arrived, it was cut short (IncompleteRead) or its status line
            # can't be read (BadStatusLine). Not the URLError text: it can carry the request
            # URL. The class name is enough.
            log.warning("%s: transport failure %s", op, type(e).__name__)
            raise SandboxError(op, "unreachable", message=type(e).__name__) from None
        try:
            out = cbor.loads(raw)
        except ValueError:
            raise SandboxError(op, "bad_response", message="answer is not CBOR") from None
        if not isinstance(out, dict):
            raise SandboxError(op, "bad_response", message="answer is not a map")
        log.info("%s: HTTP 200", op)
        return out

    def _http_error(
        self, op: str, e: urllib.error.HTTPError, bearer: str, *, rename: bool = False
    ) -> SandboxError:
        try:
            raw = e.read()
        except (http.client.HTTPException, OSError):
            raw = b""  # the error body was cut short: keep the status (and its stop rule)
        try:
            err = cbor.loads(raw) if raw else {}
        except ValueError:
            err = {}
        if not isinstance(err, dict):
            err = {}
        kind = str(err.get("__type", "")).rsplit("#", 1)[-1] or f"HTTP{e.code}"
        message = ""
        if e.code not in _QUIET_STATUSES:
            message = str(err.get("message") or err.get("Message") or "")
            message = message.replace(bearer, "<redacted>")[:_MESSAGE_MAX]
        # A rename's answer that the service's rename switch can't be read is not an outage
        # (it says try again), so it doesn't stop the client.
        switch_unreadable = rename and is_rename_switch_unreadable(op, e.code, kind, message)
        # A 5XX on ListSandboxes (a read) doesn't stop the client; the routes retry the List on
        # a capped backoff. Writes and any 429 keep the stop rule.
        list_outage = op == "ListSandboxes" and e.code >= 500
        if not switch_unreadable and not list_outage:
            self._guard.note_status(op, e.code, kind)
        log.warning("%s: HTTP %s %s", op, e.code, kind)
        error = SandboxError(op, kind, e.code, message)
        error.answer = err
        return error

    # -- the user's network setting (the step lives in netcfg_step.py) -----------------

    def netcfg_get(self) -> dict:
        """``GetNetworkConfiguration`` (bearer service): the raw answer; the caller parses it."""
        return self.call("GetNetworkConfiguration", {})

    def netcfg_set(self, mode: str, custom_domains: list, *, ignore_stop: bool = False) -> dict:
        """``UpdateNetworkConfiguration`` (portal service). The call REPLACES ``customDomains``; the caller
        carries the list it read. ``ignore_stop`` is for the undo only (it must run after a 5XX stopped the client).
        """
        body = {
            "csrfToken": "x",
            "networkConfig": {"networkAccess": mode, "customDomains": list(custom_domains)},
        }
        return self.call(
            "UpdateNetworkConfiguration", body, service=PORTAL_SERVICE_PATH, ignore_stop=ignore_stop
        )

    # -- ListSandboxes -------------------------------------------------------------

    def list_sandboxes(self) -> list[dict]:
        """All the caller's environments, from every client; nothing is stopped by a
        foreign entry."""
        body: dict = {"maxResults": LIST_PAGE_SIZE}
        entries: list[dict] = []
        for _ in range(LIST_MAX_PAGES):
            out = self.call("ListSandboxes", body)
            page = out.get("sandboxes") or []
            if not isinstance(page, list):
                raise SandboxError(
                    "ListSandboxes", "bad_response", message="sandboxes is not a list"
                )
            entries += [p for p in page if isinstance(p, dict)]
            next_page = out.get("nextToken")
            if not next_page:  # an empty page WITH a next-page marker keeps looping
                break
            body = {"maxResults": LIST_PAGE_SIZE, "nextToken": next_page}
        else:
            raise SandboxError(
                "ListSandboxes",
                "bad_response",
                message=f"still paging after {LIST_MAX_PAGES} pages",
            )
        # Only here, after every page was read: a failed or partial List never feeds it.
        source = self.accept_fingerprint
        self._guard.accept_listed(entries, source() if source is not None else "")
        return entries

    # -- Create / Get / Stop / Resume / Delete -------------------------------------

    def create_sandbox(
        self,
        display_name: str,
        *,
        idle_timeout_minutes: int | None = None,
    ) -> dict:
        """``idleTimeoutMinutes`` (CreateSandbox's own optional field) is sent only when
        given, so every other create keeps its exact shape.

        List first, the guard's create checks (``guard.before_create``), call once, record
        the id. Never retried. A refusal at the account's cap is raised with the List's two
        counts on it; an answer that was lost is recorded with the guard before its error is
        raised."""
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= DISPLAY_NAME_MAX:
            raise ValueError(f"displayName must be 1-{DISPLAY_NAME_MAX} characters")
        self._guard.check_not_stopped()
        existing = self.list_sandboxes()
        own = self._guard.own_ids()
        other = sum(1 for x in existing if x.get("execEnvironmentId") not in own)
        self._guard.before_create()
        try:
            body: dict = {"displayName": display_name}
            if idle_timeout_minutes is not None:
                body["idleTimeoutMinutes"] = idle_timeout_minutes
            out = self.call("CreateSandbox", body)
        except SandboxError as e:
            if is_quota_refusal(e.op, e.status, e.kind):
                e.listed, e.listed_other = len(existing), other
            elif e.status is None and e.kind in UNCONFIRMED_KINDS:
                self._guard.note_unconfirmed(display_name)
            raise
        self._guard.record_created(out, display_name)
        return _summary("CreateSandbox", out)

    def _require(self, exec_environment_id: str, op: str) -> None:
        """The ledger or the accepted set, or for Resume and Get an id the user asked about by
        their click (``guard.require_asked``), bound to the sign-in at call time."""
        guard = self._guard
        if guard.is_asked(exec_environment_id) and not guard.is_actionable(exec_environment_id):
            guard.require_asked(exec_environment_id, op, self.accept_fingerprint)
        else:
            guard.require_actionable(exec_environment_id, self.accept_fingerprint)

    def get_sandbox(self, exec_environment_id: str) -> dict:
        self._require(exec_environment_id, "get")
        body = {"execEnvironmentId": exec_environment_id}
        out = _summary("GetSandbox", self.call("GetSandbox", body))
        self._guard.clear_policy(exec_environment_id)  # a call for the box succeeded
        return out

    def stop_sandbox(self, exec_environment_id: str) -> dict:
        self._guard.require_actionable(exec_environment_id, self.accept_fingerprint)
        return _summary(
            "StopSandbox", self.call("StopSandbox", {"execEnvironmentId": exec_environment_id})
        )

    def resume_sandbox(self, exec_environment_id: str) -> dict:
        """Retry every 3 s, 60 s at most, on a 409 that says "not ready". Every other error,
        409 included, is final.

        After the first not-ready 409 of the call (only then), one ``GetSandbox`` of the
        same id: ``FAILED`` or ``DELETED``, or a 404, raises :class:`SandboxStateError`
        (``failed`` / ``deleted`` / ``gone``) at once; ``PENDING`` or ``RUNNING`` ends it
        with that answer and no repeat call; any other status keeps the retry. A Get error
        that is not a 404 is raised as it is."""
        self._require(exec_environment_id, "resume")
        body = {"execEnvironmentId": exec_environment_id}
        started = self._clock()
        checked = False
        while True:
            try:
                out = _summary("ResumeSandbox", self.call("ResumeSandbox", body))
                self._guard.clear_policy(exec_environment_id)  # the policy allowed the box
                return out
            except SandboxError as e:
                if not is_resume_not_ready(e):
                    raise
                if not checked:
                    checked = True
                    up = self._end_resume_if_terminal(exec_environment_id)
                    if up is not None:
                        return up  # starting or up already, so no repeat and no error
                if self._clock() - started + RESUME_RETRY_INTERVAL_S > RESUME_RETRY_WINDOW_S:
                    raise
                self._sleep(RESUME_RETRY_INTERVAL_S)

    def _end_resume_if_terminal(self, exec_environment_id: str) -> Optional[dict]:
        """The one Get after a not-ready 409: FAILED, DELETED or a 404 ends the Resume (raised);
        PENDING or RUNNING ends it too, by returning the Get's answer. None: keep retrying."""
        try:
            got = self.get_sandbox(exec_environment_id)
        except SandboxError as e:
            if not e.not_found:
                raise
            raise SandboxStateError(
                "gone", f"Resume cannot start sandbox {exec_environment_id}: it is gone"
            ) from e
        status = str(got["status"])
        if status in RESUME_END_STATUSES:
            text = f"Resume cannot start sandbox {exec_environment_id}: it is {status}"
            raise SandboxStateError(status.lower(), text, got)
        return got if status in RESUME_UP_STATUSES else None

    def delete_sandbox(self, exec_environment_id: str) -> dict:
        """Own ids only. A 404 means gone and is answered as ``DELETED``."""
        self._guard.require_own(exec_environment_id)
        try:
            out = self.call("DeleteSandbox", {"execEnvironmentId": exec_environment_id})
            result = _summary("DeleteSandbox", out)
        except SandboxError as e:
            if not e.not_found:
                raise
            result = {"execEnvironmentId": exec_environment_id, "status": "DELETED", "gone": True}
        self._guard.record_deleted(exec_environment_id)
        return result

    def update_sandbox(self, exec_environment_id: str, *, idle_timeout_minutes: int) -> dict:
        """``UpdateSandbox`` with ``{execEnvironmentId, idleTimeoutMinutes}`` and nothing else.
        Never retried. A running environment answers RUNNING until its restart begins; the
        answer keeps ``status`` and ``idleTimeoutMinutes``."""
        self._guard.require_actionable(exec_environment_id, self.accept_fingerprint)
        body = {
            "execEnvironmentId": exec_environment_id,
            "idleTimeoutMinutes": idle_timeout_minutes,
        }
        return _summary("UpdateSandbox", self.call("UpdateSandbox", body))

    def rename_sandbox(self, exec_environment_id: str, display_name: str) -> dict:
        """``UpdateSandbox`` with ``{execEnvironmentId, displayName}`` and nothing else (no idle
        timeout, so no restart). Never retried. The service decides: its rename switch off
        answers a 400, an unreadable switch a 503, which (and only which) doesn't stop the
        client."""
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= DISPLAY_NAME_MAX:
            raise ValueError(f"displayName must be 1-{DISPLAY_NAME_MAX} characters")
        self._guard.require_actionable(exec_environment_id, self.accept_fingerprint)
        body = {"execEnvironmentId": exec_environment_id, "displayName": display_name}
        return _summary("UpdateSandbox", self.call("UpdateSandbox", body, rename=True))

    # -- polling -------------------------------------------------------------------

    def wait_running(self, exec_environment_id: str, *, start_shown: bool = False) -> dict:
        """Poll GetSandbox every 10 s, at most 90 times, until RUNNING.

        Ends early (raising) on a non-200 answer (the :class:`SandboxError` itself),
        ``FAILED``, ``DELETED``, or ``STOPPED`` once a start has been shown
        (``start_shown`` when the Create or Resume answer already showed one)."""
        got: dict = {}
        for n in range(POLL_MAX):
            if n:
                self._sleep(POLL_INTERVAL_S)
            got = self.get_sandbox(exec_environment_id)
            status = got["status"]
            if status == "RUNNING":
                return got
            if status in ("FAILED", "DELETED"):
                raise SandboxStateError(status.lower(), f"sandbox is {status}", got)
            if status == "STOPPED" and start_shown:
                raise SandboxStateError("stopped_after_start", "sandbox went back to STOPPED", got)
            if status != "STOPPED":
                start_shown = True
        raise SandboxStateError("poll_timeout", f"not RUNNING after {POLL_MAX} polls", got)

    # -- StartSshSession -----------------------------------------------------------

    def start_ssh_session(self, exec_environment_id: str, *, read_ticket: bool = False) -> SshTuple:
        """A new session on every call. A 409 is surfaced as 'not RUNNING' (never resumes).

        ``read_ticket``: the ssh-session route took the one-shot ticket for the one read on
        a user-asked id; only then is that id let through (``require_asked`` op ``read``)."""
        if read_ticket:
            self._guard.require_asked(exec_environment_id, "read", self.accept_fingerprint)
        else:
            self._guard.require_actionable(exec_environment_id, self.accept_fingerprint)
        self._guard.check_policy_hold(exec_environment_id)  # no call inside the hold
        body = {"identifier": {"executionEnvironmentId": exec_environment_id}}
        try:
            out = self.call("StartSshSession", body)
        except SandboxError as e:
            if is_policy_refusal(e.op, e.status, e.kind):
                self._guard.note_policy(exec_environment_id)  # this box only; no stop
            raise
        self._guard.clear_policy(exec_environment_id)
        missing = [k for k in ("ssmSessionId", "streamUrl", "tokenValue") if not out.get(k)]
        if missing:
            raise SandboxError(
                "StartSshSession", "bad_response", message="answer missing " + ", ".join(missing)
            )
        return SshTuple(str(out["ssmSessionId"]), str(out["streamUrl"]), str(out["tokenValue"]))


def is_resume_not_ready(e: SandboxError) -> bool:
    """A Resume's 409 that says the box isn't ready (the service's text or kind): retried."""
    return e.status == 409 and (
        RESUME_NOT_READY_TEXT in e.message.lower() or e.kind == RESUME_NOT_READY_KIND
    )


def _summary(op: str, out: dict) -> dict:
    if not out.get("execEnvironmentId") or not out.get("status"):
        raise SandboxError(op, "bad_response", message="answer missing execEnvironmentId or status")
    return out


__all__ = [
    "BffClient",
    "ClientStopped",
    "GuardRefused",
    "SandboxError",
    "SandboxStateError",
    "SshTuple",
    "is_resume_not_ready",
]

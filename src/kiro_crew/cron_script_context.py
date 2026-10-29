"""The API a cron script function is handed: ``ScriptContext`` and the flow-control exceptions.

A script's ``run(ctx)`` receives a ``ScriptContext``: it calls MCP tools, posts
notifications and chat messages through the gateway, and opens sessions. It ends a
run early by raising ``Skip``, ``Done`` or ``Report``.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`). Logs under the ``kiro_crew.cron_script`` logger.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.cron_script_mcp import KeptMcpServers
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.sandbox import refuse_unaudited_on_dead_fs
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.cron import CronJob

logger = logging.getLogger("kiro_crew.cron_script")


class SkipError(Exception):
    """Abort this tick silently. Cron fires again next interval."""


class DoneError(Exception):
    """Complete the cron job. Job is removed from the schedule.

    Use ctx.notify() before raising Done() to deliver a message.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


class ReportError(Exception):
    """Deliver a message but keep the job running.

    Use for long-lived monitors that need to report multiple times.
    """

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


# Backward-compat aliases: Skip/Done/Report are the public API used by
# user-authored cron scripts. Renamed to *Error for flake8 N818; aliases
# preserve the existing import/raise surface with zero behavior change.
Skip = SkipError

Done = DoneError

Report = ReportError


@dataclass
class ScriptContext:
    """Passed to script functions. Provides delivery, tool access and session control.

    Every gateway call presents the cron's OWN credential: the internal secret,
    the ``cron:<job id>`` session key and the run's signed session token. No
    method here mints or holds a dashboard token. ``POST /api/token/local``
    refuses a sandboxed cron child on purpose, because a cron body is
    agent-writable and an owner token reaches the keystone writes under
    ``/api/security``; the methods below reach only routes the internal secret
    already serves, and that secret is not admitted to those writes.
    """

    job: CronJob
    _port: int = 5476
    _secret: str = ""
    _session_token: str = ""
    _kept_servers: KeptMcpServers = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._kept_servers = KeptMcpServers(session_key=f"cron:{self.job.id}")
        # The parent injects the port it minted the credential for. Preferring it
        # keeps credential and dial target from one resolution; KIROCREW_PORT is the
        # fallback for a directly-constructed context and is 5476 on a --port auto
        # gateway, which is a SIBLING rather than this instance.
        self._port = int(
            os.environ.pop("_KIROCREW_DIAL_PORT", "") or os.environ.get("KIROCREW_PORT", "5476")
        )
        # Secret injected via temp file (not inherited env) to prevent privilege escalation.
        # Pop env var and unlink file immediately so fn(ctx) cannot access the secret directly.
        secret_file = os.environ.pop("_KIROCREW_SECRET_FILE", "")
        if secret_file and Path(secret_file).exists():
            self._secret = Path(secret_file).read_text()
            try:
                Path(secret_file).unlink()
            except OSError:
                pass
        else:
            self._secret = os.environ.pop("KIROCREW_INTERNAL_SECRET", "")
        # The key states an identity; the token PROVES it (see
        # _publish_script_session_token). Read, not popped: the MCP servers
        # ``call_tool`` spawns inherit the same env and need the same token.
        self._session_token = os.environ.get(STUB_SESSION_TOKEN_ENV, "")

    @property
    def message(self) -> str:
        """The cron job's message field (it carries args to scripts)."""
        return getattr(self.job, "message", "")

    def notify(self, text: str, **kwargs: Any) -> dict:
        """Send a message via the gateway (same as send_message MCP tool).

        Raises RuntimeError if delivery fails.
        """
        safe_text = redact(text)
        # Redact kwargs values
        kwargs_str = json.dumps(kwargs) if kwargs else "{}"
        kwargs_str = redact(kwargs_str)
        safe_kwargs = json.loads(kwargs_str) if kwargs else {}
        payload: dict[str, Any] = {"text": safe_text, **safe_kwargs}
        # caller_session lets session="origin" resolve to the chat that created this
        # cron; hard-assigned (not setdefault) so a script cannot spoof another session
        payload["caller_session"] = f"cron:{self.job.id}"
        result = self._post("/api/send-message", payload)
        if "error" in result:
            raise RuntimeError(f"notify() failed: {result['error']}")
        return result

    # ── Dashboard sessions ──
    #
    # A dispatcher cron lists the folder it files sessions in, opens a session
    # there, seeds it with its first message and sets the session's approval
    # mode so unattended work does not wait on a prompt. Each call goes to a
    # ``/api/chat`` route the internal secret already serves, with the same
    # credential ``notify()`` presents; see the class docstring for why no
    # dashboard token is involved. A cron bound to a crew member is admitted to
    # the folder calls and refused on ``open_session``, ``send_to_session`` and
    # ``set_session_mode`` by the member chat-control gate, the same answer that
    # gate gives any member caller.

    def list_session_folders(self) -> list[dict]:
        """Return the dashboard's session folders (``GET /api/chat/folders``).

        Raises RuntimeError if the gateway refuses or cannot be reached.
        """
        result = self._exchange(
            urllib.request.Request(
                f"http://127.0.0.1:{self._port}/api/chat/folders",
                headers=self._headers(),
                method="GET",
            )
        )
        if not isinstance(result, list):
            raise RuntimeError(f"list_session_folders() failed: {self._reason(result)}")
        return result

    def create_session_folder(self, name: str) -> dict:
        """Create a session folder and return it (``POST /api/chat/folders``).

        The name is redacted the way ``notify()`` redacts its text, because it
        is rendered in the dashboard sidebar. Raises RuntimeError if the gateway
        refuses or cannot be reached.
        """
        result = self._post("/api/chat/folders", {"name": redact(name)})
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(f"create_session_folder() failed: {self._reason(result)}")
        return result

    def open_session(
        self, name: str = "", *, folder_id: str = "", agent: str = "", model: str = ""
    ) -> str:
        """Open a dashboard session and return its slot key (``POST /api/chat/slots``).

        An omitted argument is left out of the request, so the gateway applies
        its own default for it. The name is redacted the way ``notify()``
        redacts its text, because it is rendered in the dashboard sidebar.
        When ``agent.session_control`` is false the gateway refuses with
        ``session_control_disabled``, and this method raises RuntimeError
        carrying that code. Raises RuntimeError if the gateway refuses or
        cannot be reached.
        """
        body = {
            key: value
            for key, value in (
                ("name", redact(name)),
                ("title", redact(name)),
                ("folder_id", folder_id),
                ("agent", agent),
                ("model", model),
            )
            if value
        }
        result = self._post("/api/chat/slots", body)
        key = result.get("key") if isinstance(result, dict) else None
        if not isinstance(key, str) or not key or "error" in result:
            raise RuntimeError(f"open_session() failed: {self._reason(result)}")
        return key

    def send_to_session(self, slot: str, message: str) -> dict:
        """Queue *message* as the next user turn on *slot* (``POST /api/chat?ws=1``).

        The turn runs on the gateway; this returns the receipt as soon as the
        message is accepted instead of streaming the reply. An idle slot answers
        ``{"ok": True, "slot": <key>}`` and starts the turn. A slot that is busy
        answers ``{"ok": True, "queued": True, "queue_id": <id>}`` and runs the
        turn when its current one ends, so read ``slot`` with ``.get()``. The
        message is redacted the way ``notify()`` redacts its text. When
        ``agent.session_control`` is false the gateway refuses with
        ``session_control_disabled``, and this method raises RuntimeError
        carrying that code. Raises RuntimeError if the gateway refuses or
        cannot be reached.
        """
        result = self._post("/api/chat?ws=1", {"slot": slot, "message": redact(message)})
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(f"send_to_session() failed: {self._reason(result)}")
        return result

    def set_session_mode(self, slot: str, mode: str) -> dict:
        """Set the tool approval mode of *slot* (``POST /api/chat/mode``).

        *mode* is ``"trust"`` (auto-approve every tool on that session) or
        ``"trust_reads"`` (auto-approve read-only tools). Both are scoped to the
        one session named and leave the process-global override alone. The
        gateway refuses any other mode, ``yolo`` and ``normal`` included, with
        ``mode_not_allowed``, a slot this cron did not open with ``not_creator``,
        and the call itself with ``session_control_disabled`` while
        ``agent.session_control`` is false; it audits each call and each refusal.
        The mode is sent as given, so the gateway, not this method, is the one
        place that rule lives. Returns the gateway's receipt, ``{"ok": True,
        "mode": <mode>}``. Raises RuntimeError carrying the gateway's code if it
        refuses or cannot be reached.
        """
        result = self._post("/api/chat/mode", {"slot": slot, "mode": mode})
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(f"set_session_mode() failed: {self._reason(result)}")
        return result

    # ── The crewmate's crew log ──
    #
    # A script cron bound to a crewmate feeds that crewmate's Dashboard page without a
    # model turn: each record it appends lands in the crewmate's own crew log as one
    # ``dashboard/custom_value`` entry, and the ``custom`` fold keeps the latest record
    # per type, so a page bound to ``custom.latest.<type>.data`` redraws when it lands.

    def crew_log_append(self, type: str, data: dict) -> dict:
        """Append one record to the bound crewmate's crew log (``POST .../dashboard/record``).

        *type* names the record: letters, digits and underscores only, at most 64
        characters, so a page binds it as ``custom.latest.<type>``; *data* is a JSON
        object of at most 8 KiB
        serialized and 8 levels deep. The record is redacted the way ``notify()``
        redacts its text, because a page renders it.

        The gateway decides WHO wrote it: the entry is stamped with this job's id and
        the crewmate it is bound to, read from the job's own attested identity, and
        nothing in the request can change either. A job bound to no crewmate is
        refused ``cron_not_member_bound``; a crewmate whose chat has never run has no
        crew log yet and answers ``crew_log_unavailable``; more than 30 records in a
        minute answers ``quota_exceeded``. Returns the gateway's receipt, ``{"ok":
        True, "recorded": {"type", "writer", "member"}}``. Raises RuntimeError
        carrying the gateway's reason if it refuses or cannot be reached.
        """
        # Each string on its own, never the serialized blob: a credential pattern that
        # spans a key and its value (``"authorization": "Bearer ..."``) would otherwise
        # eat the ``": "`` between them and leave text that is not JSON.
        safe = {"type": redact(type), "data": _redact_deep(data)}
        result = self._post("/api/agent-panel/dashboard/record", safe)
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(f"crew_log_append() failed: {self._reason(result)}")
        return result

    @staticmethod
    def _reason(result: object) -> str:
        if isinstance(result, dict) and result.get("error"):
            return str(result["error"])
        return f"unexpected response {json.dumps(result)[:200]}"

    def call_tool(self, server: str, tool: str, args: dict) -> str:
        """Call an MCP tool, starting the server subprocess on the first call to it.

        The server lives for the run, not for one call: :class:`KeptMcpServers`
        keeps a server that answered the call (with a result or a tool error)
        for this run's next call to it, so a server that signs in to a service
        when it starts signs in once per run rather than once per call.
        :meth:`close` stops the kept servers.

        Args are scanned for credential/URL leakage before passing to the
        sandboxed MCP server subprocess.
        """
        # Scan serialized args for credential patterns
        args_str = json.dumps(args)
        args_str = redact(args_str)
        safe_args = json.loads(args_str)
        try:
            result = self._kept_servers.call_tool(server, tool, safe_args)
        except Exception as exc:
            self._audit_tool_call(server, tool, "error", str(exc))
            raise
        self._audit_tool_call(server, tool, "ok")
        return result

    def close(self) -> None:
        """Stop every MCP server kept for reuse; later calls keep none.

        The launcher calls this once the script function returns. It never
        raises, because by then the run's result is already decided.
        """
        self._kept_servers.close()

    def _audit_tool_call(self, server: str, tool: str, outcome: str, error: str = "") -> None:
        """Log tool invocation for audit trail."""
        logger.info(
            "cron_script tool_call: job=%s server=%s tool=%s outcome=%s%s",
            self.job.id,
            server,
            tool,
            outcome,
            f" error={error}" if error else "",
        )
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{self.job.id}",
                tool_name=f"{server}/{tool}",
                tool_kind="cron_script_tool",
                outcome=outcome,
                error=error,
                # Synchronous, so a write failure reaches this handler instead of
                # being swallowed by the background writer.
                critical=True,
            )
        except Exception as exc:
            # A cron child whose audit write hits ENOSYS refuses to go on.
            refuse_unaudited_on_dead_fs(exc, "cron script tool-call audit")
            logger.debug("SEL audit logging failed in cron_script tool call", exc_info=True)

    def _post(self, path: str, body: dict) -> dict:
        """POST *body* to *path* as this cron, decoded; ``{"error": ...}`` on failure."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        return self._exchange(req)

    def _headers(self) -> dict[str, str]:
        """The cron's whole credential, on every call.

        The internal secret proves the loopback process, the ``cron:<job id>``
        key names the job, and the signed token attests the key.
        """
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": self._secret,
            "X-Session-Key": f"cron:{self.job.id}",
        }
        if self._session_token:
            headers["X-Session-Token"] = self._session_token
        return headers

    @staticmethod
    def _exchange(req: urllib.request.Request) -> Any:
        """Send one built loopback request; decoded JSON, or ``{"error": ...}``.

        An HTTP refusal keeps the gateway's own reason, redacted, because
        ``HTTP Error 403: Forbidden`` alone hides the remedy the gateway names
        in its body. It also carries ``status_code`` and, when the body names
        one, the gateway's ``code``, so a script that calls ``_post`` itself can
        tell a refused identity (409 ``member_identity_unavailable``) from a job
        that is already running (409 with no ``code``) without parsing the
        ``error`` text. A transport failure has neither and stays
        ``{"error": str(exc)}``.
        """
        where = f"{req.get_method()} {req.selector}"
        try:
            with loopback_urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = ""
            detail = ""
            try:
                body = exc.read().decode("utf-8", "replace")
                detail = redact(body)[:500]
            except Exception:
                pass
            logger.warning("ScriptContext %s refused: HTTP %s", where, exc.code)
            refusal: dict[str, Any] = {
                "error": f"HTTP {exc.code}: {detail or exc.reason}",
                "status_code": exc.code,
            }
            code = _refusal_code(body)
            if code:
                refusal["code"] = code
            return refusal
        except Exception as exc:
            logger.warning("ScriptContext %s failed: %s", where, exc)
            return {"error": str(exc)}


def _redact_pair(key: str, value: str) -> str:
    """*value* redacted as the value UNDER *key*.

    A credential pattern can need its key to match: ``Bearer <token>`` alone passes the
    redactor, while ``authorization: Bearer <token>`` is caught. Redacting the value on
    its own would let that pair through and the token would render on a page, and
    redacting the serialized record would eat the delimiter between the two and leave
    text that is not JSON. So the value is judged twice: alone, and then the cleaned
    result spelled beside its key the way a header line spells it. A pair the second
    judgement catches is answered
    with the redactor's own tag in the value's place, so the key stays and the shape holds.
    """
    safe = redact(value)
    # On the CLEANED value, always: a value carrying two tokens -- one the bare scrub
    # catches and one only its key reveals -- must not keep the second for the first.
    spelled = f"{key}: {safe}"
    if redact(spelled) != spelled:
        return REDACTED_CREDENTIAL_TAG
    return safe


def _redact_deep(value: Any, key: str = "") -> Any:
    """*value* with every string in it redacted, shape kept.

    Strings are redacted one at a time -- keys and values alike, because a record's
    keys are the script's own free text too -- and containers are rebuilt around them.
    A string value is judged with the key it sits under (:func:`_redact_pair`). A key
    that redacts onto one already present is suffixed rather than dropped, so two
    distinct members do not collapse into one. Scalars that are not strings pass through.
    """
    if isinstance(value, str):
        return _redact_pair(key, value) if key else redact(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        # One counter per colliding base, so N keys that redact to one name cost N
        # steps rather than a search that restarts for each.
        suffixes: dict[Any, int] = {}
        for raw_key, inner in value.items():
            name = redact(raw_key) if isinstance(raw_key, str) else raw_key
            if name in out:
                base = name
                suffix = suffixes.get(base, 1) + 1
                while f"{base} ({suffix})" in out:
                    suffix += 1
                suffixes[base] = suffix
                name = f"{base} ({suffix})"
            out[name] = _redact_deep(inner, raw_key if isinstance(raw_key, str) else "")
        return out
    if isinstance(value, list):
        return [_redact_deep(inner, key) for inner in value]
    return value


_REFUSAL_CODE = re.compile(r"[a-z0-9_.-]{1,64}")


def _refusal_code(body: str) -> str:
    """The gateway's machine-readable ``code`` from a refusal body, or ``""``.

    The body is text this process did not write, so a code survives only as a
    short identifier; anything else, and any body that does not parse as a
    JSON object (:func:`parse_json_object_line` answers ``None`` for text that
    is not JSON, for a scalar or a list, and for a body nested past the
    parser's limit), yields no code.
    """
    parsed = parse_json_object_line(body)
    code = parsed.get("code") if parsed is not None else None
    if isinstance(code, str) and _REFUSAL_CODE.fullmatch(code):
        return code
    return ""


# A traceback names these by the path callers import them from.
SkipError.__module__ = "kiro_crew.cron_script"
DoneError.__module__ = "kiro_crew.cron_script"
ReportError.__module__ = "kiro_crew.cron_script"

"""The tools first-party apps declare in their manifests, listed and forwarded, never run here.

``apps.app_agent_tools`` reads each first-party app's ``agentTools`` block. This domain
turns it into ``kirocrew-core`` tools without running any app code in this process,
which holds the gateway's internal secret:

- ``schemas()`` lists every tool a shipped first-party manifest declares, installed or
  not: it is the names build an app agent's deny universe reads, and that universe is
  fixed when the agent is materialized. ``advertised()`` lists the tools of the
  installed first-party apps that are not switched off (hidden only on a readable
  ``False``, listed on a read that fails);
- ``HANDLERS`` answers each declared name with ONE core handler, :func:`app_agent_tool`,
  which posts the validated call to the gateway's ``/api/app-tools/call``, waits the
  tool's declared ``timeoutSeconds``, and returns the answer redacted and cut at the
  tool's declared cap: as JSON (an answer of exactly ``{"text": <string>}`` is that
  text), or as the app's own text for a tool declared ``result: "text"``, which keeps
  verbatim only a credential the answer names. A tool declared ``strictSession`` is
  posted only from a strictly identified session, with that session's key.

It is the LAST domain in ``DOMAIN_MODULES``, and it drops any declared name another
domain already has: a core tool always keeps its name, its handler and its schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.mcp_tools.catalog.app_tools import (  # noqa: F401
    _DOMAIN,
    _core_tool_names,
    _tools,
    _universe,
    advertised,
    app_enabled_state,
    schemas,
)
from kiro_crew.platform import redact_via_context as redact

#: The gateway route every forwarded call reaches: strict internal, so only a process
#: holding the internal secret may call it (``dashboard/handlers/app_agent_tools.py``).
_CALL_PATH = "/api/app-tools/call"
#: A credential a text answer keeps verbatim: one printable token, never prose, and long
#: enough that cutting the text at it cannot split an ordinary word or a short secret.
_CREDENTIAL_RE = re.compile(r"[!-~]{16,4096}")
#: The most pieces a credential may cut a text answer into: it stands there 7 times at most.
_MAX_CREDENTIAL_PIECES = 8
#: A refusal's ``code`` is named in its error line only in this shape, as the gateway
#: client keeps it.
_CODE_RE = re.compile(r"[a-z0-9_.-]{1,64}")


def declared(name: str) -> app_agent_tools.DeclaredTool | None:
    """The app tool called *name*, or ``None`` when no app owns that name."""
    if name in _core_tool_names():
        return None
    return app_agent_tools.declared_tool(name)


def tool_schema(name: str) -> Any:
    """The validation schema of app tool *name*, or ``None`` when it is not one."""
    tool = declared(name)
    return app_agent_tools.build_tool_schema(tool) if tool is not None else None


#: What a ``strictSession`` tool answers when this session cannot be identified directly.
_STRICT_SESSION_REFUSAL = (
    "Error: this tool needs a directly-identified dashboard session. A subagent "
    "resolves to its parent's session, which would act as the parent. Run this "
    "from the session the work belongs to."
)


def _answer_text(resp: Any) -> str:
    """The tool result for the app's answer: its ``text`` when that is all it sent, else JSON."""
    if isinstance(resp, dict) and set(resp) == {"text"} and isinstance(resp["text"], str):
        return resp["text"]
    return json.dumps(resp, ensure_ascii=False, default=str)


def _is_credential(value: Any) -> bool:
    return isinstance(value, str) and _CREDENTIAL_RE.fullmatch(value) is not None


def _text_answer(name: str, resp: dict[str, Any]) -> str:
    """The model-facing text of a ``result: "text"`` tool's answer *resp*.

    ``{"text": <str>}`` is that text, redacted. A tool whose answer hands over a
    credential it exists to deliver (a token scoped to something the agent just
    started) answers ``{"text": [<str>, ...], "credential": <str>}``: the text with the
    credential cut out at each place it stands. Each piece is redacted on its own and
    the credential is put back between them, so the one string the app names survives
    verbatim and every other character of the reply is redacted as a JSON answer is.

    Anything else is a failure and reads as one, an ``Error:`` line the tool-call audit
    records as failed: ``{"error", "code"?}`` is a refusal the gateway, the app or the
    transport produced (a timeout, a gateway that is restarting).
    """
    text, credential = resp.get("text"), resp.get("credential")
    if set(resp) == {"text"} and isinstance(text, str):
        return redact(text)
    if (
        set(resp) == {"text", "credential"}
        and _is_credential(credential)
        and isinstance(text, list)
        and 2 <= len(text) <= _MAX_CREDENTIAL_PIECES
        and all(isinstance(piece, str) for piece in text)
    ):
        return str(credential).join(redact(piece) for piece in text)
    if resp.get("error"):
        code = resp.get("code")
        suffix = f" [{code}]" if isinstance(code, str) and _CODE_RE.fullmatch(code) else ""
        return f"Error: {name} failed{suffix}: {redact(str(resp['error']))}"
    return f"Error: {name} returned no result: {redact(json.dumps(resp, default=str))}"


def app_agent_tool(name: str, args: dict[str, Any]) -> str:
    """Forward one call of an app's declared tool to the gateway, which hands it to the app.

    *args* has already passed the declared schema (``mcp_core._validate_args`` reads
    :func:`tool_schema`). The gateway checks it again, resolves the app that declares
    *name* and posts ``{"tool", "arguments"}`` to that app's declared route. A
    ``strictSession`` tool is posted with the strictly resolved session key, and not at
    all when there is none. The answer echoes app state no inbound scrub touched, so it
    is redacted here, then cut at the tool's declared cap: redacting first means a cut
    can never split a credential. An app that answers ``{"text": <string>}`` and
    nothing else gets that text as the result, so a tool can answer in prose.

    A tool declared ``result: "text"`` answers with the text it formatted for the
    model, which is redacted and capped the same way (:func:`_text_answer`).
    """
    tool = declared(name)
    if tool is None:
        return f"Error: no installed app declares the tool {name!r}."
    body = {"tool": name, "arguments": args}
    if tool.decl.strict_session:
        # The key the gate returned is the key on the wire: re-resolving at the post
        # through the lenient walk would check one session and act as another.
        session_key, refusal = mcp_core.require_strict_session_key(_STRICT_SESSION_REFUSAL)
        if not session_key:
            return refusal
        resp = mcp_core._post(
            _CALL_PATH, body, timeout=tool.decl.timeout_seconds, session_key=session_key
        )
    else:
        resp = mcp_core._post(_CALL_PATH, body, timeout=tool.decl.timeout_seconds)
    if tool.decl.result == "text":
        text = _text_answer(name, resp)
    else:
        text = redact(_answer_text(resp))
    cap = tool.decl.max_result_chars
    if len(text) > cap:
        text = text[:cap] + f"\n… truncated ({len(text)} chars total)."
    return text


class _DeclaredHandlers(Mapping[str, Any]):
    """``HANDLERS`` for a tool set read from manifests: every name ``schemas()`` lists maps to one handler.

    A tool whose app is not installed has the handler too, which answers with an error
    and posts nothing.
    """

    def __getitem__(self, name: str) -> Any:
        if name not in self:
            raise KeyError(name)
        return app_agent_tool

    def __contains__(self, name: object) -> bool:
        return any(t.name == name for t in _universe())

    def __iter__(self) -> Iterator[str]:
        return iter([t.name for t in _universe()])

    def __len__(self) -> int:
        return len(_universe())


HANDLERS: Mapping[str, Any] = _DeclaredHandlers()

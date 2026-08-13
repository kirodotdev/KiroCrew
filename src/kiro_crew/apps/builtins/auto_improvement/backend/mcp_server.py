"""A stdio MCP server exposing the run's state as read-only agent tools.

Why stdio and not HTTP: the upstream app ran as its own process on an allocated
port and served MCP over that port. A Kiro Crew builtin runs IN-PROCESS inside the
gateway, so it has no backend port of its own — and the app bridge deliberately
SKIPS a URL-based MCP entry when there is no live backend port, precisely so a
dead default-port URL cannot poison every session's provider config. A command
(stdio) entry has no port to be dead, so that is the shape a builtin must use.

It reads the same on-disk artifacts the HTTP routes read, through the same
modules, so there is exactly one source of truth for what a finding or a ruler
is. Nothing here mutates state: an agent inspecting a run must not be able to
start, stop, or re-file anything, because those are the operator's decisions and
the loop's own gates. Read-only also means these tools are safe to auto-approve.

Run as: ``python -m kiro_crew.apps.builtins.auto_improvement.backend.mcp_server``
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from kiro_crew.security import redact
from kiro_crew.sel import sel

from . import deps, progress, runner

#: JSON-RPC 2.0 error codes used here.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

#: Protocol version echoed on initialize.
_PROTOCOL_VERSION = "2024-11-05"

#: Cap on a single tool result. An agent asking for a finding must not be able to
#: pull an unbounded diff into its context and blow the window.
_MAX_RESULT_CHARS = 60_000


def _redact_result(text: str) -> str:
    """Credential/exfil-scan a serialized tool result before it reaches the LLM.

    FAIL-CLOSED: these six tools are conveniences (an agent can read the same artifacts
    off disk), so withholding a result beats handing a credential to a model whose output
    may be logged, echoed into a PR body, or sent to a provider.
    """
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - never emit unscanned run evidence
        print("auto-improvement mcp: result redaction failed; withholding", file=sys.stderr)
        return '{"error": "result withheld: redaction unavailable"}'


def _redact_error(text: str) -> str:
    """Scrub an error string before it reaches the SEL record or the model.

    Tool RESULTS were already scanned while the ERROR paths beside them were not, and tool
    ARGUMENTS reach exception text by design (``_tool_get_finding`` raises "no finding with
    fingerprint <fp>" with the caller's raw value). Measured: `get_finding` with a
    credential-shaped `fp` echoed it back verbatim in the JSON-RPC error.

    FAIL-CLOSED for the same reason as :func:`_redact_result`: an unscannable message becomes
    a fixed string rather than being handed to a model whose output may be logged or echoed
    into a PR body. The caller still learns that the call failed, and the tool name and
    JSON-RPC error CODE — which carry the actionable part — are never derived from input.
    """
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - never emit unscanned text
        return "withheld: redaction unavailable"


def _audit(
    tool_name: str,
    *,
    outcome: str,
    request_id: Any = "",
    error: str = "",
    critical: bool = False,
) -> None:
    """Record one ``tools/call`` in the Security Event Log.

    Every dispatch is audited, including the rejected ones — a run of calls to names
    that are not tools is what probing looks like, and dropping those would leave the
    interesting case unlogged.

    ``critical=True`` is the "audit-or-deny" contract: the event is written synchronously
    and a filesystem failure is re-raised so the caller refuses the call rather than
    serving it untraced. The pre-dispatch ``invoked`` event uses it (see ``tools/call``);
    the OUTCOME events deliberately do not, because by the time those fire the handler has
    already run — raising there would turn an audit-sink problem into a failed call without
    preventing anything.

    Why the invocation is audit-or-deny even though all six tools are pure reads: the
    criterion ``sel`` itself states is ATTENDEDNESS, not blast radius — "pass
    ``critical=True`` when the caller enforces audit-or-deny (e.g. an unattended heartbeat
    auto-approve)". This server is exactly that shape: no human is in the loop and the
    results are handed to an LLM, so "which run artifacts did the agent read, and when" is
    the only record that a read ever happened. An earlier revision of this docstring argued
    reads were too harmless to gate; that weighed impact instead of the repo's own rule, and
    the review that kept pressing on it was right.

    A non-critical audit failure degrades to a stderr note — stderr, not stdout, because
    stdout is the JSON-RPC channel and a stray line there would corrupt the protocol.
    """
    try:
        sel().log_tool_invocation(
            session_key="auto-improvement-mcp",
            agent="auto-improvement",
            source="auto_improvement_mcp",
            tool_name=tool_name,
            tool_kind="read",
            outcome=outcome,
            request_id=str(request_id or ""),
            error=error,
            critical=critical,
        )
    except Exception as exc:  # noqa: BLE001 - shape depends on `critical`, see below
        if critical:
            # Audit-or-deny: let the caller turn this into a refusal.
            raise
        print(f"auto-improvement mcp: SEL audit failed: {exc}", file=sys.stderr)


def _tool_get_status(_args: dict[str, Any]) -> dict[str, Any]:
    """Whether a run is active, and its headline counters."""

    return runner.get_supervisor().status()


def _tool_get_ruler(_args: dict[str, Any]) -> dict[str, Any]:
    """The active ruler and whether it is calibrated."""

    ruler = progress.read_ruler()
    return {"ruler": ruler, "calibrated": progress.ruler_calibrated()}


def _tool_list_findings(args: dict[str, Any]) -> dict[str, Any]:
    """Ledger entries, newest first. ``status`` filters to one status."""

    findings = progress.read_findings()
    wanted = str(args.get("status") or "").strip()
    if wanted:
        findings = [f for f in findings if str(f.get("status") or "") == wanted]
    limit = args.get("limit")
    try:
        n = int(limit) if limit is not None else 50
    except (TypeError, ValueError):
        n = 50
    return {"findings": findings[: max(1, min(n, 200))], "total": len(findings)}


def _tool_get_finding(args: dict[str, Any]) -> dict[str, Any]:
    """One finding's evidence: signature, hypothesis, gate results, PR reference.

    Deliberately omits the diff — an agent that wants the change can read the
    repository; shipping a large diff through a tool result mostly burns context.
    """
    fp = str(args.get("fp") or "").strip()
    if not fp:
        raise ValueError("fp is required")

    rows = [f for f in progress.read_findings() if str(f.get("fp") or "") == fp]
    if not rows:
        raise ValueError(f"no finding with fingerprint {fp}")
    latest = rows[0]
    return {
        "fp": fp,
        "kind": latest.get("kind") or "",
        "target": latest.get("target") or "",
        "status": latest.get("status") or "",
        "note": latest.get("note") or "",
        "pr": latest.get("pr") or "",
        "history": [str(r.get("status") or "") for r in reversed(rows)],
    }


def _tool_get_progress(_args: dict[str, Any]) -> dict[str, Any]:
    """The cumulative-best series: one point per measured candidate."""

    return progress.read_progress()


def _tool_get_deps(_args: dict[str, Any]) -> dict[str, Any]:
    """Which external tools a run needs and whether they are present."""

    return deps.check_deps()


#: Read-only tools, with the minimal schema ``tools/list`` needs. Every one is
#: safe to auto-approve; there is deliberately no write tool here.
TOOLS: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], str, dict[str, Any]]] = {
    "get_status": (_tool_get_status, "Whether a run is active, and its counters.", {}),
    "get_ruler": (_tool_get_ruler, "The metric being optimized and its trust state.", {}),
    "list_findings": (
        _tool_list_findings,
        "Findings the run recorded, newest first.",
        {
            "status": {"type": "string", "description": "Filter to one ledger status."},
            "limit": {"type": "integer", "description": "Max rows (default 50, cap 200)."},
        },
    ),
    "get_finding": (
        _tool_get_finding,
        "One finding's evidence and pull-request reference.",
        {"fp": {"type": "string", "description": "The finding's fingerprint."}},
    ),
    "get_progress": (
        _tool_get_progress,
        "The cumulative-best progress series for the run.",
        {},
    ),
    "get_deps": (_tool_get_deps, "External tool availability for a run.", {}),
}


def _input_schema(name: str) -> dict[str, Any]:
    """The declared ``inputSchema`` for one tool.

    Factored out so ``tools/list`` and the argument validator cannot disagree — a validator
    checking a different shape than the one advertised is worse than no validator.
    """
    _fn, _description, props = TOOLS[name]
    required = ["fp"] if name == "get_finding" else []
    return {"type": "object", "properties": props, "required": required}


def _schema() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, (_fn, description, props) in TOOLS.items():
        out.append(
            {
                "name": name,
                "description": description,
                "inputSchema": _input_schema(name),
            }
        )
    return out


def _result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns None for a notification."""
    method = str(request.get("method") or "")
    req_id = request.get("id")
    raw_params = request.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "auto-improvement", "version": "1.0.0"},
            },
        )
    if method in {"notifications/initialized", "initialized"}:
        return None  # a notification carries no id and expects no reply
    if method == "tools/list":
        return _result(req_id, {"tools": _schema()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        # The tool NAME is caller-supplied and reaches two readers that must never carry raw
        # untrusted text: the SEL record (persisted, HMAC-signed as-written, not redacted by the
        # writer) and the JSON-RPC error returned to the model. A credential-shaped name
        # (`get_finding` proved arguments echo back; the name is the same surface) would leak
        # through both on the reject paths below. Redact ONCE and use the safe value for every
        # audit/error mention; the RAW `name` is used only for the `TOOLS` allowlist lookup,
        # which is an exact-match dict get and never emitted. Raised by the GPT review.
        safe_name = _redact_error(name)
        entry = TOOLS.get(name)
        if entry is None:
            # An unknown name is rejected before any dispatch: TOOLS is the allowlist,
            # so a caller cannot reach a handler that is not in it. Audited too — a
            # sweep of names that are not tools is what probing looks like.
            _audit(safe_name, outcome="denied", request_id=req_id, error="unknown_tool")
            return _error(req_id, _METHOD_NOT_FOUND, f"unknown tool: {safe_name}")
        raw_args = params.get("arguments")
        # REJECT a present-but-non-dict `arguments` rather than coercing it to `{}`. The
        # coercion made a malformed call (`"arguments": [1,2]` or a bare string) succeed
        # with `result` for any no-required-arg tool instead of returning INVALID_PARAMS —
        # the schema validator only ran against the already-emptied dict, so it never saw
        # the violation. Absent/None stays valid (arguments are optional). Raised by the
        # GPT review of this branch.
        if raw_args is not None and not isinstance(raw_args, dict):
            _audit(safe_name, outcome="denied", request_id=req_id, error="invalid_arguments")
            return _error(req_id, _INVALID_PARAMS, "invalid arguments: expected an object")
        args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
        # VALIDATE against the tool's declared inputSchema before dispatch. Coercing a
        # non-dict to `{}` and handing whatever survives to the handler meant a malformed
        # call could reach a tool and come back as `result` rather than INVALID_PARAMS —
        # and an unknown key was silently ignored instead of refused. The shared validator
        # is fail-closed for exactly this boundary (untrusted `tools/call`), so use it
        # rather than a local check that would drift from the schema in `_schema()`.
        # Raised by review of this branch.
        # Stays function-local, unlike `redact`/`sel` which were hoisted: this one has an
        # explicit `except ImportError` fallback, which is the `top-level-imports` rule's
        # documented optional-dependency exemption. Hoisting it would make an absent
        # validator an import-time crash of the whole server instead of a skipped check.
        try:
            from kiro_crew.validation import validate_mcp_tool_arguments

            validate_mcp_tool_arguments(args, _input_schema(name))
        except ImportError:  # pragma: no cover - the validator ships with the package
            pass
        except Exception as exc:  # noqa: BLE001 - a schema violation is a client error
            # Redacted like the other error paths: a schema validator quotes the OFFENDING
            # VALUE, which is caller-supplied, so this reaches the model too. Not named by
            # review, but the same surface — fixing only the reported sites is how the next
            # one drifts back in.
            _audit(safe_name, outcome="denied", request_id=req_id, error="invalid_arguments")
            return _error(req_id, _INVALID_PARAMS, f"invalid arguments: {_redact_error(str(exc))}")
        fn = entry[0]
        # Audit the DISPATCH before running the handler, so the record of "this tool was
        # invoked" cannot be lost by whatever the handler does. Previously only the
        # outcome was audited, all of it after `fn(args)` returned — so a handler that
        # died hard (a killed process, an interpreter-level failure, anything that does
        # not surface as an exception here) executed with no audit trail at all. The
        # outcome events below still fire; this one makes the invocation itself
        # unconditional. Raised by the GPT review of this branch.
        # AUDIT-OR-DENY. `critical=True` writes synchronously and re-raises on a
        # filesystem failure, so a call that cannot be recorded is REFUSED instead of
        # served untraced. No human is in this loop and the result goes to an LLM, so this
        # event is the only evidence the read happened.
        try:
            _audit(safe_name, outcome="invoked", request_id=req_id, critical=True)
        except Exception:  # noqa: BLE001 - unauditable call must not be served
            # Fixed diagnostic only: neither the caller-derived tool name nor the exception text
            # is interpolated here. `safe_name` IS redacted, but interpolating a value tainted by
            # request input into a log line trips CodeQL's clear-text-logging taint check (it
            # does not model `_redact_error` as a sanitizer), and the exception could itself
            # carry sensitive text. The tool name is not needed to act on this — the SEL is down,
            # which is the whole message — and the JSON-RPC reply below tells the caller. The
            # exception is deliberately swallowed rather than logged, for the same reason the
            # result/error paths route everything through the redactor.
            print(
                "auto-improvement mcp: refusing a tool call — the security audit log is "
                "unavailable",
                file=sys.stderr,
            )
            return _error(req_id, _INTERNAL_ERROR, "refused: the security audit log is unavailable")
        try:
            payload = fn(args)
        except ValueError as exc:
            # Redacted for BOTH readers: the SEL record and the model-facing response. The
            # exception TYPE is composed in after scrubbing, so the message stays useful.
            detail = _redact_error(str(exc))
            _audit(safe_name, outcome="error", request_id=req_id, error=f"invalid_params: {detail}")
            return _error(req_id, _INVALID_PARAMS, detail)
        except Exception as exc:  # noqa: BLE001 - a tool error is a result, not a crash
            detail = f"{type(exc).__name__}: {_redact_error(str(exc))}"
            _audit(safe_name, outcome="error", request_id=req_id, error=detail)
            return _error(req_id, _INTERNAL_ERROR, detail)
        _audit(safe_name, outcome="success", request_id=req_id)
        # Redact BEFORE truncating: every tool result is agent-authored run evidence (a
        # finding's signature/hypothesis/note come from the model's own prose) and it is
        # handed to an LLM. This is the same class of text `routes._redact_for_display`
        # scans for the browser and `runner._redact_activity` scans for the feed; the MCP
        # surface was the remaining reader. Measured before fixing: a credential in a
        # ledger note came back verbatim in `list_findings`. Raised by review of this
        # branch.
        #
        # Order matters — truncating first could split a credential across the cut and
        # leave a fragment the scanner no longer recognizes.
        text = _redact_result(json.dumps(payload, default=str))[:_MAX_RESULT_CHARS]
        return _result(req_id, {"content": [{"type": "text", "text": text}]})
    return _error(req_id, _METHOD_NOT_FOUND, f"unknown method: {method}")


def main() -> None:
    """Read line-delimited JSON-RPC on stdin, write replies on stdout.

    One malformed line must not end the session: the client may still send valid
    requests afterwards, and dying here would surface as the whole MCP server
    disappearing mid-session.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        reply = handle(request)
        if reply is None:
            continue
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

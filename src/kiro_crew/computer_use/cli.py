"""``kirocrew computer`` — human-facing computer-use diagnostics and a debug driver.

Three subcommands:

* ``doctor [--json]`` — platform support, the keystone primary enable, and the
  ADVISORY macOS permission probe. ``--json`` is the machine form the dashboard
  shells out for its Settings permission rows, which is how the native
  accessibility probe stays OUT of the gateway process: a ctypes fault in a
  short-lived subprocess costs one diagnostic, while the same fault in-gateway
  would take down every session, the cron scheduler, the Slack socket and every
  dashboard websocket at once.
* ``apps`` — the on-screen application list, so a user can see the names and
  bundle ids the agent will match against.
* ``call`` — run ONE computer-use tool, or a JSON array of them **in a single
  process**, against the same dispatch chokepoint the agent uses. See below.

Deliberately NO ``kirocrew computer state <app>``. That would be a second,
CLI-shaped spelling of an LLM-facing capability, and the MCP-first rule requires
an LLM-facing capability to be an MCP tool (it is: ``computer_get_state``).
``doctor`` is a permission diagnostic rather than a capability, and ``apps`` has
its MCP twin (``computer_list_apps``), so neither brushes the rule.

**Why ``call`` does not brush the MCP-first rule either.** It is not a new
capability and it adds no tool: it is a *harness* over the existing ten, for a
human reproducing a failure at a terminal. The rule exists so the model gets a
structured tool rather than being told to shell out — and the model already has
all ten as MCP tools. ``call`` deliberately has no MCP twin, because a tool that
runs other tools would let a model launder a per-call gate decision through one
approved invocation.

**``call`` is fully gated, and that is the point of routing it through
``tools.dispatch_tool`` rather than reaching into ``service``.** Every call goes
through the same ordered chokepoint as an agent call: the keystone primary enable,
the fail-closed ``gate.require_computer_use``, the built-in app denylist, index
freshness, the secure-target refusals, and the observation ceiling. So this
command cannot be used to see or do anything the agent could not, which is
exactly what makes it a faithful reproduction tool. Two consequences worth
stating rather than discovering:

* the session key is the attended CLI surface (:data:`_CLI_SESSION_KEY`) — a real
  surface the gate accepts, not a bypass sentinel;
* ``approval_recorded`` is left at ``False``. The approval ceiling it used to
  satisfy is gone, so the flag no longer changes any outcome — but it is still
  never minted here, because doing so would be the CLI asserting a prompt that
  nobody answered on this leg (asserted by an AST test over the whole package).

**Why a whole array in one process.** ``element_index`` values only mean anything
relative to the ``computer_get_state`` that produced them, and the cache that
holds that mapping (``index.SnapshotIndex``, reached via the shared service
singleton) is per-process with a 90s TTL. Two ``kirocrew computer call``
invocations therefore cannot share indices at all — the second would refuse with
"no state for …". ``--calls`` exists so a snapshot-then-act sequence is
reproducible from one command line, which is the shape the reference
implementation's ``call --calls`` has for the same reason.

Hand-rolled dispatch mirroring ``browser/cli.py`` rather than argparse
subparsers: the command surface is two words plus free-form arguments, and the
parent CLI already forwards ``REMAINDER``.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Mapping

from kiro_crew.computer_use import enable_state, service, tools
from kiro_crew.computer_use.backend import platform_id_for_current_os
from kiro_crew.computer_use.policy import blocked_app_categories
from kiro_crew.computer_use.types import (
    ERROR_PREFIX,
    PERMISSION_UNKNOWN,
    STATE_KEY_ENABLED,
    TOOL_LIST_APPS,
)

logger = logging.getLogger(__name__)

# Exit codes. ``doctor`` reports a real status, so a script can gate on it; the
# JSON form always exits 0 because its consumer reads the body.
_EXIT_OK = 0
_EXIT_PROBLEM = 1

# The surface identity ``call`` presents to the gate. ``cli_chat`` is the repo's
# existing key for "a human at a terminal" — ``sel._infer_source`` maps it to the
# ``cli`` surface, so the SEL audit record attributes the call correctly. Reusing it
# (rather than minting a private key) is what keeps the audit trail readable and lets
# an operator profile bound to the CLI surface govern this command too.
#
# It is NOT an authorization input: there is no unattended-surface refusal to
# avoid. ``computer_use.gate`` has no ``UNATTENDED_SURFACES`` /
# ``UNATTENDED_KEY_PREFIXES`` — the only ``_UNATTENDED_SURFACES`` in the tree is
# ``platform/governance_profiles.py``'s, which selects a PROFILE and carries no
# computer-use rule.
_CLI_SESSION_KEY = "cli_chat"


def _session_key() -> str:
    """The identity to gate this invocation with — always the attended CLI surface.

    ``cli_chat`` is the repo's existing key for "a human at a terminal"
    (``sel._infer_source`` maps it to the ``cli`` surface). It is used
    unconditionally now: the unattended-surface refusal that used to make this
    decision load-bearing is gone, so there is nothing left for a stricter identity
    to buy. The key still matters for the SEL audit trail, which is why this is a
    named surface rather than an empty string.
    """
    return _CLI_SESSION_KEY


# Shown when an unauthenticated invocation has no keystone opt-in. Names the flag
# and the file, because "refused" without the remedy is not a usable diagnostic.

# ``--calls`` entry keys. A batch entry is ``{"tool": "...", "args": {...}}``;
# ``args`` is optional so a no-argument tool is ``{"tool": "computer_end_turn"}``.
_CALL_KEY_TOOL = "tool"
_CALL_KEY_ARGS = "args"
_CALL_KEYS: frozenset[str] = frozenset({_CALL_KEY_TOOL, _CALL_KEY_ARGS})

_USAGE = """kirocrew computer — desktop automation (computer use) diagnostics

Commands:
  doctor           Show platform support, whether computer use is enabled, and
                   the macOS Accessibility / Screen Recording permission hints
  doctor --json    The same report as JSON (used by the dashboard)
  apps             List applications with an on-screen window
  call <tool> [key=value ...]
                   Run one computer-use tool and print its reply
  call --calls '[{"tool": "...", "args": {...}}, ...]'
                   Run several tools in ONE process, so element_index values from
                   an earlier computer_get_state are still valid for later calls
  call ... --json  Emit the replies as a JSON array instead of prose

A key=value argument is parsed as JSON when it can be (element_index=3,
screenshot=false, x=120.5) and kept as a plain string otherwise (app=Finder).
Wrap a value with spaces in shell quotes: text='hello there'.

Every call runs through the same gate the agent does — the primary enable,
security policy, the app denylist and the secure-field refusals all apply, so
this cannot reach anything the agent could not. Under a policy that forces
interactive approval, a mutating call is refused here: there is no prompt on this
leg for anyone to answer.

Computer use is OFF by default and can only be enabled by you, from the
dashboard: Settings -> Computer Use. An agent cannot enable it.
"""


def main() -> None:
    """Console entry point (``kirocrew computer ...``)."""
    run_computer(sys.argv[1:])


def run_computer(args: list[str]) -> None:
    """Entry point for ``kirocrew computer <subcommand>``."""
    if not args:
        print(_USAGE)
        return
    cmd = args[0]
    if cmd in ("-h", "--help", "help"):
        print(_USAGE)
        return
    if cmd == "doctor":
        _cmd_doctor(as_json="--json" in args[1:])
        return
    if cmd == "apps":
        _cmd_apps()
        return
    if cmd == "call":
        _cmd_call(args[1:])
        return
    print(f"Unknown command: {cmd}. Run 'kirocrew computer' for help.", file=sys.stderr)
    sys.exit(_EXIT_PROBLEM)


def _cmd_doctor(*, as_json: bool) -> None:
    """Print the support/enable/permission report."""
    report = build_doctor_report()
    if as_json:
        # Exit 0 regardless: the dashboard reads the body, and a non-zero exit
        # would make it treat a legitimately-unsupported platform as a probe
        # failure and render "unknown" instead of the real reason.
        print(json.dumps(report, indent=2))
        return

    print(f"Platform:      {report['platform']}")
    if report["supported"]:
        print("Supported:     yes")
    else:
        print(f"Supported:     no — {report['reason']}")
    print(f"Enabled:       {'yes' if report['enabled'] else 'no (Settings -> Computer Use)'}")

    perms = report["permissions"]
    if report["platform"] == "macos":
        print(f"Accessibility: {perms['accessibility']}")
        print(f"Screen record: {perms['screen_recording']}")
        if perms.get("responsible_hint"):
            print(f"Grant to:      {perms['responsible_hint']}")
        # Stated every time, not only on a miss. macOS attributes a TCC grant to
        # the RESPONSIBLE PARENT of the process tree, so a probe reporting
        # "missing" while a full-fidelity capture succeeds is normal — and a user
        # who believes the probe will chase a permission they already have.
        print()
        print(
            "Note: these permission readings are advisory. macOS attributes a\n"
            "grant to the process that launched KiroCrew, so 'missing' does not\n"
            "always mean unavailable — and computer use is never gated on them."
        )

    if report["blocked_apps"]:
        print()
        print("Always-blocked targets (built in, not configurable):")
        for entry in report["blocked_apps"]:
            print(f"  - {entry['category']}: {entry['reason']}")

    if report["errors"]:
        print()
        for message in report["errors"]:
            print(f"Problem: {message}", file=sys.stderr)
        sys.exit(_EXIT_PROBLEM)
    sys.exit(_EXIT_OK if report["supported"] else _EXIT_PROBLEM)


def build_doctor_report() -> dict:
    """Assemble the doctor report. Never raises.

    Every probe is individually guarded and its failure recorded in ``errors``:
    this function is what the dashboard shells out to, and it must produce a
    usable payload on a machine where the accessibility framework will not load
    at all. A missing probe degrades to ``unknown``, never to ``granted``.
    """
    errors: list[str] = []
    platform_id = platform_id_for_current_os()

    enabled = False
    try:
        enabled = enable_state.is_enabled()
    except Exception as exc:
        errors.append(f"could not read the computer-use state file: {exc}")

    supported = False
    reason = ""
    permissions = {
        "accessibility": PERMISSION_UNKNOWN,
        "screen_recording": PERMISSION_UNKNOWN,
        "responsible_hint": "",
    }
    try:
        svc = service.get_shared_service()
        status = svc.status()
        supported = status.supported
        reason = status.reason
        platform_id = status.platform_id or platform_id
        probe = svc.probe_permissions()
        permissions = {
            "accessibility": probe.accessibility,
            "screen_recording": probe.screen_recording,
            "responsible_hint": probe.responsible_hint,
        }
    except Exception as exc:
        # A driver that will not even construct is exactly what this command
        # exists to report, so name it rather than crashing.
        errors.append(f"the computer-use driver could not be probed: {exc}")
        reason = reason or str(exc)

    return {
        "platform": platform_id,
        "supported": supported,
        "reason": reason,
        "enabled": enabled,
        "state_file": str(_state_path()),
        "state_key": STATE_KEY_ENABLED,
        "permissions": permissions,
        "blocked_apps": [dict(entry) for entry in blocked_app_categories()],
        "errors": errors,
    }


def _state_path() -> object:
    """The keystone state path, or a placeholder when it cannot be resolved."""
    try:
        return enable_state.computer_use_state_path()
    except Exception:
        return "(unresolved)"


def _cmd_apps() -> None:
    """Print the on-screen application list, through the SAME gate as ``call``.

    This used to call ``service.list_apps()`` directly, on the
    reasoning that a diagnostic in the operator's own terminal is not the agent.
    That reasoning does not hold, because the agent can run this command with
    bash — so the direct call was an ungated read of every window TITLE (document
    names, paths, and whatever a terminal put in its title) that worked even with
    the feature disabled, in an unattended cron session, or under a policy that
    bans computer use outright.

    Routing it through ``dispatch_tool`` costs the operator nothing they should
    have had: ``computer_list_apps`` renders the same list, filtered by the app
    denylist and the observation ceiling. If the answer is a refusal, that refusal
    IS the diagnostic — and ``kirocrew computer doctor`` (which reads only the
    keystone and the TCC state, never the window list) is still the ungated way to
    find out why the feature is off.
    """
    print(tools.dispatch_tool(TOOL_LIST_APPS, {}, session_key=_session_key()))


def _cmd_call(argv: list[str]) -> None:
    """Run one tool, or a ``--calls`` batch, and print the replies.

    Exits non-zero when ANY reply is an error, so a shell can gate on it. The
    check is the ``Error: `` prefix rather than an exception, because
    ``dispatch_tool`` is contracted to return every refusal as text — the prefix
    is the same load-bearing marker ``mcp_shared.call_tool_with_logging``
    classifies a failed SEL outcome from.

    Batch execution is SEQUENTIAL and does **not** stop at the first error. A
    reproduction is more useful whole: seeing that step 2 was refused and step 3
    then hit a stale index is the actual story, whereas aborting would hide the
    second half of it.
    """
    as_json = "--json" in argv
    rest = [arg for arg in argv if arg != "--json"]

    try:
        calls = _parse_calls(rest)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(_EXIT_PROBLEM)

    # AFTER parsing (so a malformed command still teaches the syntax) but before any
    # dispatch: an unauthenticated caller with no keystone opt-in gets ONE clear
    # refusal rather than the same one repeated for every entry of a batch.

    # Resolved ONCE for the whole batch: re-reading the keystone per call would let a
    # concurrent Settings change split one batch across two ceilings.
    session_key = _session_key()
    results: list[dict[str, Any]] = []
    for tool_name, tool_args in calls:
        # ``dispatch_tool`` never raises except for ``PlatformCompositionError``
        # (a host that cannot compose its platform context must NOT degrade to a
        # text refusal), so that one propagates out of this command too and the
        # rest of the batch is abandoned — an un-composable ceiling is not a
        # per-call failure.
        text = tools.dispatch_tool(tool_name, tool_args, session_key=session_key)
        results.append({_CALL_KEY_TOOL: tool_name, "text": text})

    failed = any(str(entry["text"]).startswith(ERROR_PREFIX) for entry in results)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for position, entry in enumerate(results):
            if len(results) > 1:
                # Only label in a batch: a single call's reply should be pasteable
                # as-is, and a header would corrupt a copy of the rendered tree.
                print(f"── {position + 1}/{len(results)} {entry[_CALL_KEY_TOOL]} ──")
            print(entry["text"])
    if failed:
        sys.exit(_EXIT_PROBLEM)


def _parse_calls(argv: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Turn the argv tail into an ordered ``(tool_name, args)`` list.

    Two accepted forms, and never a mix — ``--calls`` carries its own tool names,
    so a positional tool beside it is ambiguous about ordering and is rejected
    rather than guessed at:

    * ``--calls '<json array>'`` — the batch form;
    * ``<tool> [key=value ...]`` — the single form.

    Raises :class:`ValueError` with a user-facing sentence for every malformed
    input. Argument TYPES are deliberately not checked here: the per-tool schema
    in ``MCP_COMPUTER_SCHEMAS`` is the one authority on them, and re-stating it
    would create a second, drifting copy. This function only produces the shape
    ``dispatch_tool`` takes.
    """
    if not argv:
        raise ValueError("call needs a tool name, or --calls with a JSON array")

    if argv[0] == "--calls":
        if len(argv) < 2:
            raise ValueError("--calls needs a JSON array argument")
        if len(argv) > 2:
            raise ValueError("--calls takes exactly one JSON array; quote it as one argument")
        return _parse_batch(argv[1])
    if "--calls" in argv:
        raise ValueError("use either --calls or a positional tool name, not both")

    tool_name = argv[0]
    if tool_name.startswith("-"):
        raise ValueError(f"expected a tool name, got the flag {tool_name!r}")
    return [(tool_name, _parse_kv_args(argv[1:]))]


def _parse_batch(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse the ``--calls`` JSON array into ``(tool_name, args)`` pairs."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--calls is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError('--calls must be a JSON array of {"tool": ..., "args": ...} objects')
    if not parsed:
        raise ValueError("--calls array is empty")

    calls: list[tuple[str, dict[str, Any]]] = []
    for position, entry in enumerate(parsed):
        label = f"--calls entry {position + 1}"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a JSON object")
        unknown = sorted(set(entry) - _CALL_KEYS)
        if unknown:
            # Rejected rather than ignored: a misspelled ``arg``/``arguments`` key
            # would otherwise run the tool with NO arguments, which for a real
            # desktop action is a silently different call, not a no-op.
            raise ValueError(f"{label} has unknown key(s): {', '.join(unknown)}")
        tool_name = entry.get(_CALL_KEY_TOOL)
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(f"{label} needs a non-empty string 'tool'")
        tool_args = entry.get(_CALL_KEY_ARGS, {})
        if tool_args is None:
            tool_args = {}
        if not isinstance(tool_args, dict):
            raise ValueError(f"{label} 'args' must be a JSON object")
        calls.append((tool_name, dict(tool_args)))
    return calls


def _parse_kv_args(tokens: list[str]) -> dict[str, Any]:
    """Parse ``key=value`` tokens, JSON-decoding each value when possible.

    ``element_index=3`` must arrive as an int and ``screenshot=false`` as a bool,
    because the schema rejects the string forms — so a bare ``json.loads`` is
    tried first. It is a FALLBACK, not a requirement: ``app=Finder`` is not valid
    JSON and must stay the string ``"Finder"``, and ``text=null`` staying the
    four-character string it looks like at a shell prompt is the less surprising
    reading of a typed argument. Quote a value to force the string form
    (``app='"Finder"'`` is the same thing either way).
    """
    parsed: dict[str, Any] = {}
    for token in tokens:
        if token.startswith("-"):
            raise ValueError(f"unknown flag {token!r}; arguments are key=value")
        key, sep, raw = token.partition("=")
        if not sep or not key:
            raise ValueError(f"expected key=value, got {token!r}")
        if key in parsed:
            raise ValueError(f"{key} given twice")
        parsed[key] = _coerce(raw)
    return parsed


def _coerce(raw: str) -> Any:
    """JSON-decode *raw* when it parses to a non-string scalar, else keep the text.

    A JSON string/array/object literal is kept as TEXT on purpose: a value that
    already decodes to a ``str`` gains nothing from the round trip, and letting a
    ``key=[1,2]`` build a real list would hand the schema a container no
    computer-use field accepts, turning a typo into a confusing type error rather
    than a plain "unknown field"/length refusal.
    """
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(value, (bool, int, float)) and not isinstance(value, str):
        return value
    return raw


def run_calls(calls: "list[tuple[str, Mapping[str, Any]]]") -> list[str]:
    """Run *calls* in this process and return their replies, in order.

    The programmatic form of ``call --calls``, exposed for a test (and for a future
    harness) so the batch semantics — one process, one snapshot cache, sequential,
    no abort on error — can be exercised without capturing stdout or catching
    :func:`sys.exit`.

    ``PlatformCompositionError`` propagates, exactly as it does through
    ``dispatch_tool``.
    """
    session_key = _session_key()
    return [tools.dispatch_tool(name, dict(args), session_key=session_key) for name, args in calls]


__all__ = ["build_doctor_report", "main", "run_calls", "run_computer"]

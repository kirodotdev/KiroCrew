#!/usr/bin/env python3
"""Drive Kiro Agent (KAS) through one small native workflow run and record the frames.

``ACP_BACKENDS_NATIVE_WORKFLOWS`` in ``src/kiro_crew/agent_sdk/backends_routing.py``
admits a harness whose OWN workflow engine Crew drives over the ``_kiro/workflow/*``
extension methods (:mod:`kiro_crew.kas_workflows`). The harness-parity X-capture rule
(``docs/ci/harness-parity-gate.md``) holds that set to a LIVE capture of KAS itself,
so this script is that capture, as one command:

    python3 scripts/capture_kas_workflow.py
    python3 scripts/capture_kas_workflow.py --out build/kas-workflow-live.jsonl

It spawns KAS the way Crew's production relay does (``kiro-cli acp --agent-engine v3
--auth-method cli``), declares the client capabilities Crew declares while the Kiro
Workflows switch is on, and then, in a scratch project:

1. ``session/new`` with ``_meta.kiro.settings.workflows.enabled: true``
   (:func:`kiro_crew.kas_workflows.chat.attach_workflow_settings`);
2. writes a two-step workspace recipe and reads it back with
   ``_kiro/workflow/listRecipes``;
3. ``_kiro/workflow/new`` + ``_kiro/workflow/invoke`` with the params
   :func:`kiro_crew.kas_workflows.wire.new_run_params` builds;
4. answers every request the run sends: a step's ``_kiro/userInput`` question with
   its first option, a ``session/request_permission`` with its allow-once option;
5. waits for the run's terminal ``_kiro/workflow/run_complete``, then reads it with
   ``_kiro/workflow/load``, ``inspect`` and ``list``, and waits for the parent turn
   KAS starts on its own when a run ends (``session_info_update`` ``turn_start`` /
   ``turn_end``, :func:`kiro_crew.acp.kas_wire.turn_boundary`).

The step prompts ask the first step to send its parent a note (``send_message`` ->
``_kiro/session/notify``) and the second to ask one structured question
(``user_input`` -> ``_kiro/userInput``), so the capture holds every non-standard
frame class the workflow router reads.

What it writes
--------------
Agent-to-client lines only, verbatim and in order, in the corpus's fixture shape
(``test/fixtures/acp_frames/README.md``), with these reductions, each named in the
header's note:

* host inventory frames are omitted (MCP status, tool tags, powers, steering,
  progressive context, the session list, governance, config options, the command
  list, context-usage ``session_info_update``s) -- they describe the recording HOST,
  and ``scripts/check_acp_frame_host_data.py`` refuses them;
* model prose (``agent_thought_chunk``, ``agent_message_chunk``) and the step
  prompts KAS echoes (``user_message_chunk``) are omitted, which keeps the capture
  under the corpus's frame cap;
* the scratch project reads as ``/work`` and the home directory as ``~``;
* every uuid, session id and workflow id is replaced with a fixed synthetic value,
  consistently, so the frames still cross-reference each other.

It does not authenticate anything: kiro-cli resolves its own sign-in. It does not
write into the committed corpus: the default output is under ``build/`` (git-ignored),
because a stray ``.jsonl`` in ``test/fixtures/acp_frames/`` is collected by
``test/test_acp_frame_replay.py`` and fails for want of a snapshot. Moving a reviewed
capture in is a separate step.

Exit codes: ``0`` captured and commit-ready · ``2`` usage or environment error (kiro-cli
missing, no KAS package) · ``3`` KAS refused (initialize unanswered, auth error, a
request error) · ``4`` the run never ended inside ``--timeout`` · ``5`` written, but a
recording-host marker survived the sweep.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Importing ``kiro_crew`` builds the metrics provider; a script is not pytest, so pin
# telemetry off before the import (``update_acp_frame_snapshots.py`` does the same).
os.environ["KIROCREW_TELEMETRY"] = "0"
sys.path.insert(0, str(REPO_ROOT / "src"))

from kiro_crew.acp.types import KAS_CLIENT_CAPABILITIES, kas_client_capabilities  # noqa: E402
from kiro_crew.kas_workflows import wire  # noqa: E402
from kiro_crew.kas_workflows.chat import attach_workflow_settings  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_TIMEOUT = 4
EXIT_MARKERS = 5

#: The production relay spawn (``acp.kas_transport``): kiro-cli's v3 engine, with
#: kiro-cli owning authentication.
RELAY_ARGV = ("acp", "--agent-engine", "v3", "--auth-method", "cli")

#: How many times the drive answers a step that parked for input before it gives up
#: and lets ``--timeout`` end the drive.
MAX_REPLIES = 2

RECIPE_NAME = "kirocrew-capture"
RECIPE: dict[str, Any] = {
    "name": RECIPE_NAME,
    "description": "Two tiny steps: a note to the parent, then a question for the user.",
    "steps": [
        {
            "type": "step",
            "id": "note",
            "agent": "wf-coder",
            "prompt": (
                "Do not read, write or run anything. Send your parent one message with "
                "the send_message tool, severity info, text 'capture note'. Then finish "
                "the step by reporting success with the single word ok."
            ),
        },
        {
            "type": "step",
            "id": "ask",
            "agent": "wf-coder",
            "prompt": (
                "Do not read, write or run anything. Ask the user 'Pick a colour' with "
                "exactly two options, Red and Blue, using the user_input tool if you have "
                "it. Once you have the answer, finish the step by reporting success with "
                "the colour."
            ),
        },
    ],
}

#: Notification methods that are inventories of the recording host, not product shape.
DROPPED_METHODS = frozenset(
    {
        "_kiro/mcp/status",
        "_kiro/tools/didChange",
        "_kiro/powers/items_changed",
        "_kiro/steering/documents_changed",
        "_kiro/progressive_context/items_changed",
        "_kiro/sessions/changed",
        "_kiro/governance/state",
    }
)
#: Also dropped: the model's prose (thinking, message chunks) and the step prompts
#: KAS echoes as ``user_message_chunk``. No workflow parser reads them, and they are
#: most of a run's lines: kept, the capture passes the corpus's frame cap
#: (``test_acp_frame_replay.py`` ``_MAX_FRAMES``).
DROPPED_UPDATES = frozenset(
    {
        "available_commands_update",
        "config_option_update",
        "agent_thought_chunk",
        "agent_message_chunk",
        "user_message_chunk",
    }
)
#: Keys of the session/new answer that list what the signed-in account can use.
SESSION_INVENTORY = frozenset({"models", "configOptions", "modes"})
#: ``session_info_update`` kinds that are product shape (the engine's own turn
#: boundaries). Every other ``session_info_update`` is a context-usage breakdown that
#: names the host's files, and is dropped.
KEPT_INFO_KINDS = frozenset({"turn_start", "turn_end"})

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_WORKFLOW_ID = re.compile(r"\bwf_[0-9a-f]{16}\b")


class Refused(Exception):
    """KAS would not do what the drive needs."""


class Driver:
    """One KAS process over stdio: send requests, answer its requests, log its lines."""

    def __init__(self, argv: list[str], cwd: Path, frame_timeout: float) -> None:
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.frame_timeout = frame_timeout
        self.frames: list[dict[str, Any]] = []
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        self.questions = 0
        self.permissions = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.frames.append(msg)
                self.inbox.put(msg)

    def _write(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def send(self, method: str, params: dict[str, Any]) -> int:
        """Send a request without waiting for its answer (it is read like any line)."""
        rid = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        print(f"[{time.strftime('%H:%M:%S')}] -> {method}", file=sys.stderr, flush=True)
        return rid

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        rid = self.send(method, params)
        deadline = time.monotonic() + (timeout or self.frame_timeout)
        while True:
            msg = self.pump(deadline)
            if msg is None:
                raise Refused(f"{method} was not answered")
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise Refused(f"{method} answered an error: {msg['error']}")
                return msg.get("result")

    def pump(self, deadline: float) -> dict[str, Any] | None:
        """The next agent line before *deadline*, answering it first when it is a request."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            msg = self.inbox.get(timeout=remaining)
        except queue.Empty:
            return None
        method = msg.get("method")
        if isinstance(method, str) and method.startswith(("_kiro/workflow/", "_kiro/session/")):
            print(f"[{time.strftime('%H:%M:%S')}] <- {method}", file=sys.stderr, flush=True)
        if "method" in msg and "id" in msg:
            print(f"[{time.strftime('%H:%M:%S')}] <- request {method}", file=sys.stderr, flush=True)
            self._answer(msg)
        return msg

    def _answer(self, msg: dict[str, Any]) -> None:
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "_kiro/userInput":
            self.questions += 1
            options = params.get("options") or []
            first = options[0].get("title") if options and isinstance(options[0], dict) else "ok"
            result: dict[str, Any] = {"action": "answered", "answer": first or "ok"}
        elif method == "session/request_permission":
            self.permissions += 1
            options = params.get("options") or []
            allow = next(
                (o.get("optionId") for o in options if o.get("kind") == "allow_once"),
                options[0].get("optionId") if options else None,
            )
            result = {"outcome": {"outcome": "selected", "optionId": allow}}
        else:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32601, "message": f"{method} is not served"},
                }
            )
            return
        self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def close(self) -> None:
        try:
            self.proc.kill()
        finally:
            self.proc.wait(timeout=10)


def _run_complete(msg: dict[str, Any], workflow_id: str) -> bool:
    params = msg.get("params") or {}
    return (
        msg.get("method") == wire.NOTIFY_RUN_COMPLETE
        and params.get("workflowId") == workflow_id
        and params.get("status") in wire.TERMINAL_RUN_STATUSES
    )


def _parked_step(final_state: Any) -> dict[str, Any] | None:
    """The step node a paused run's ``finalState`` holds parked, or None."""
    stack = [final_state.get("root")] if isinstance(final_state, dict) else []
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("type", wire.NODE_TYPE_STEP) == wire.NODE_TYPE_STEP and (
            node.get("status") == wire.STATUS_PAUSED and not node.get("children")
        ):
            return node
        stack.extend(node.get("children") or [])
    return None


def _parent_woke(frames: list[dict[str, Any]], parent: str) -> bool:
    """Whether *parent* has had a whole engine-started turn (``turn_start`` .. ``turn_end``)."""
    started = False
    for msg in frames:
        if (msg.get("params") or {}).get("sessionId") != parent:
            continue
        kind = _info_kind(msg)
        if kind == "turn_start":
            started = True
        elif kind == "turn_end" and started:
            return True
    return False


def _info_kind(msg: dict[str, Any]) -> str | None:
    update = (msg.get("params") or {}).get("update") or {}
    if update.get("sessionUpdate") != "session_info_update":
        return None
    kiro = (update.get("_meta") or {}).get("kiro") or {}
    return kiro.get("kind") if isinstance(kiro, dict) else None


def drive(driver: Driver, work: Path, run_timeout: float) -> dict[str, Any]:
    """The whole drive; returns facts for the header note."""
    init = driver.call(
        "initialize",
        {
            "clientInfo": {"name": "kirocrew", "version": "capture"},
            "protocolVersion": 1,
            "clientCapabilities": kas_client_capabilities(KAS_CLIENT_CAPABILITIES, user_input=True),
        },
        timeout=120,
    )
    methods = wire.extension_methods((init or {}).get("agentCapabilities"))
    if methods is not None and wire.METHOD_NEW not in methods:
        raise Refused("this KAS does not advertise _kiro/workflow/new")
    params: dict[str, Any] = {"cwd": str(work), "mcpServers": []}
    attach_workflow_settings(params)
    session = driver.call("session/new", params, timeout=120)
    parent = (session or {}).get("sessionId")
    if not parent:
        raise Refused("session/new returned no sessionId")

    recipe_dir = work / ".kiro" / "workflows"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe_path = recipe_dir / f"{RECIPE_NAME}.workflow.json"
    recipe_path.write_text(json.dumps(RECIPE, indent=2), encoding="utf-8")
    driver.call(wire.METHOD_LIST_RECIPES, {"workspacePaths": [str(work)]})

    created = driver.call(
        wire.METHOD_NEW,
        wire.new_run_params(
            source=str(recipe_path),
            parent_session_id=parent,
            inputs={},
            run_label="capture",
        ),
    )
    workflow_id = wire.as_str((created or {}).get("workflowId"))
    if workflow_id is None:
        raise Refused("_kiro/workflow/new created no run")
    driver.call(wire.METHOD_INVOKE, {"workflowId": workflow_id})

    deadline = time.monotonic() + run_timeout
    status = None
    step_sessions: dict[str, str] = {}
    replies = 0
    while True:
        msg = driver.pump(deadline)
        if msg is None:
            raise TimeoutError(f"run {workflow_id} did not end within {run_timeout:g}s")
        params = msg.get("params") or {}
        started_node = wire.as_str(params.get("nodeId"))
        started_session = wire.as_str(params.get("sessionId"))
        if msg.get("method") == wire.NOTIFY_NODE_START and started_node and started_session:
            step_sessions[started_node] = started_session
        if _run_complete(msg, workflow_id):
            status = params["status"]
            break
        if (
            msg.get("method") == wire.NOTIFY_RUN_COMPLETE
            and params.get("workflowId") == workflow_id
            and params.get("status") == wire.STATUS_PAUSED
            and replies < MAX_REPLIES
        ):
            # A step parked for input. Crew's reply to a parked step is a plain
            # session/prompt on the STEP's session (wire.NODE_ACTION_REPLY), which
            # KAS reroutes into the run.
            parked = _parked_step(params.get("finalState")) or {}
            parked_node = wire.as_str(parked.get("nodeId"))
            session_id = (step_sessions.get(parked_node) if parked_node else None) or (
                wire.as_str(parked.get("sessionId"))
            )
            if session_id:
                replies += 1
                driver.send(
                    wire.METHOD_SESSION_PROMPT,
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": "Blue"}]},
                )

    driver.call(wire.METHOD_LOAD, {"workflowId": workflow_id})
    driver.call(wire.METHOD_INSPECT, {"workflowId": workflow_id})
    # The scoped list Crew's router sends (``KasWorkflowRouter`` lists by project).
    driver.call(wire.METHOD_LIST, {"workspacePaths": [str(work)]})

    # The parent turn KAS starts on its own (a waking note, or the run's end). It
    # may already have started during the run; waited for, not required, because
    # a busy or gated parent gets the nudge buffered instead.
    wake_deadline = time.monotonic() + min(run_timeout, 180.0)
    while not _parent_woke(driver.frames, parent):
        if driver.pump(wake_deadline) is None:
            break
    woke = _parent_woke(driver.frames, parent)
    return {
        "status": status,
        "replies": replies,
        "woke": woke,
        "questions": driver.questions,
        "permissions": driver.permissions,
    }


def reduce_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The slice the corpus keeps: product shape, not host inventory or thinking."""
    out = []
    for msg in frames:
        method = msg.get("method")
        if method in DROPPED_METHODS:
            continue
        update = (msg.get("params") or {}).get("update")
        if method == "session/update" and isinstance(update, dict):
            kind = update.get("sessionUpdate")
            if kind in DROPPED_UPDATES:
                continue
            if kind == "session_info_update" and _info_kind(msg) not in KEPT_INFO_KINDS:
                continue
        result = msg.get("result")
        if isinstance(result, dict) and "sessionId" in result:
            # session/new's answer: its model catalog, config options and mode list
            # (which names the host's own agents) are the account's and host's
            # inventory. The session id and the ``_meta`` that echoes
            # ``workflowsEnabled`` are kept.
            msg = {**msg, "result": {k: v for k, v in result.items() if k not in SESSION_INVENTORY}}
        out.append(msg)
    return out


def scrub(frames: list[dict[str, Any]], work: Path, home: Path) -> str:
    """Render *frames* as JSONL with host paths and minted ids replaced, consistently."""
    text = "\n".join(json.dumps(f, ensure_ascii=False) for f in frames)
    for real, alias in ((str(work.resolve()), "/work"), (str(work), "/work")):
        text = text.replace(real, alias)
    for real in {str(home.resolve()), str(home)}:
        text = text.replace(real, "~")
    sessions: dict[str, str] = {}

    def _session(match: re.Match[str]) -> str:
        return sessions.setdefault(match.group(0), f"kas-wf-session-{len(sessions) + 1}")

    text = re.sub(r"sess_" + _UUID.pattern, _session, text)
    ids: dict[str, str] = {}

    def _uuid(match: re.Match[str]) -> str:
        return ids.setdefault(match.group(0), f"kas-wf-id-{len(ids) + 1}")

    text = _UUID.sub(_uuid, text)
    runs: dict[str, str] = {}

    def _run(match: re.Match[str]) -> str:
        return runs.setdefault(match.group(0), f"wf_{len(runs) + 1:016x}")

    return _WORKFLOW_ID.sub(_run, text) + "\n"


def host_markers(text: str) -> list[tuple[str, str]]:
    """Markers ``scripts/check_acp_frame_host_data.py`` would refuse, as ``(match, why)``."""
    gate = REPO_ROOT / "scripts" / "check_acp_frame_host_data.py"
    spec = importlib.util.spec_from_file_location("acp_frame_host_data_gate", gate)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {gate}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = []
    for pattern, why in module.PATTERNS:
        match = re.search(pattern, text)
        if match:
            found.append((match.group(0), why))
    return found


def versions(kiro_cli: str, home: Path) -> tuple[str, str]:
    """``(kiro_cli_version, agent_version)``: the engine version is the ``@kiro/agent``
    package.json kiro-cli installed for ITS version, not kiro-cli's own."""
    out = subprocess.run([kiro_cli, "--version"], capture_output=True, text=True, timeout=30)
    match = re.search(r"(\d+\.\d+\.\d+)", out.stdout)
    if not match:
        raise RuntimeError(f"cannot read the kiro-cli version from {out.stdout!r}")
    cli = match.group(1)
    kas_root = home / ".local" / "share" / "kiro-cli" / "kas"
    for pkg in sorted(kas_root.glob(f"{cli}-*/node_modules/@kiro/agent/package.json")):
        version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
        if version:
            return cli, str(version)
    raise RuntimeError(f"no @kiro/agent package.json for kiro-cli {cli} under {kas_root}")


def _question_path(facts: dict[str, Any]) -> str:
    """How the question step asked, which depends on the step agent's tools."""
    if facts.get("questions"):
        return "The question step asked through a _kiro/userInput request."
    return (
        "The question step's agent had no user_input tool, so it asked by parking "
        "(send_message warning -> node_paused, paused, run_complete status paused)."
    )


def meta_header(cli: str, agent: str, facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "_meta": {
            "backend": "kas",
            "recorded": "live",
            "date": _datetime.date.today().isoformat(),
            "agent_version": agent,
            "kiro_cli_version": cli,
            "note": (
                f"KAS {agent} (the @kiro/agent package.json kiro-cli {cli} installs) reached "
                "through `kiro-cli acp --agent-engine v3 --auth-method cli`, the production "
                "relay spawn, driven over stdio by scripts/capture_kas_workflow.py: "
                "session/new with _meta.kiro.settings.workflows.enabled, a two-step "
                "workspace recipe (a parent note, then a question for the user) listed, "
                "created and invoked, the run's end, then load/inspect/list and the parent "
                f"turn KAS starts itself. {_question_path(facts)} A step that parks for "
                "input is answered with a session/prompt on its own session, Crew's reply "
                "path. Run ended "
                f"{facts['status']}; parked-step replies {facts['replies']}; questions answered "
                f"{facts['questions']}; permissions answered {facts['permissions']}; parent "
                f"woke {facts['woke']}. Agent-to-client lines verbatim and in order, except: "
                "host inventory frames are omitted (_kiro/mcp/status, _kiro/tools/didChange, "
                "_kiro/powers/items_changed, _kiro/steering/documents_changed, "
                "_kiro/progressive_context/items_changed, _kiro/sessions/changed, "
                "_kiro/governance/state, available_commands_update, config_option_update, "
                "context-usage session_info_update, and the models/configOptions/modes "
                "inventory of the session/new answer), model prose and echoed step "
                "prompts (agent_thought_chunk, agent_message_chunk, user_message_chunk) "
                "are omitted, the scratch "
                "project reads as /work and the home directory as ~, and every uuid, "
                "session id and workflow id is a fixed synthetic value."
            ),
        }
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=REPO_ROOT / "build" / "kas-workflow-live.jsonl")
    p.add_argument("--kiro-cli", default=shutil.which("kiro-cli") or "kiro-cli")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds for the run to end")
    p.add_argument("--frame-timeout", type=float, default=90.0, help="seconds per request")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    home = Path.home()
    try:
        cli, agent = versions(args.kiro_cli, home)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    work = Path(tempfile.mkdtemp(prefix="kas-wf-capture-"))
    driver = Driver([args.kiro_cli, *RELAY_ARGV], work, args.frame_timeout)
    code = EXIT_OK
    facts: dict[str, Any] = {}
    try:
        facts = drive(driver, work, args.timeout)
    except Refused as exc:
        print(f"KAS refused: {exc}", file=sys.stderr)
        code = EXIT_REFUSED
    except TimeoutError as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        code = EXIT_TIMEOUT
    finally:
        driver.close()
    if code != EXIT_OK:
        raw = args.out.with_suffix(".raw.jsonl")
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("\n".join(json.dumps(f) for f in driver.frames) + "\n", encoding="utf-8")
        print(f"unreduced frames (host data, do not commit): {raw}", file=sys.stderr)
        return code
    body = scrub(reduce_frames(driver.frames), work, home)
    text = json.dumps(meta_header(cli, agent, facts), ensure_ascii=False) + "\n" + body
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({body.count(chr(10))} frames); {facts}")
    markers = host_markers(text)
    for match, why in markers:
        print(f"host marker survived: {match!r} ({why})", file=sys.stderr)
    return EXIT_MARKERS if markers else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

"""Run recorded ACP turns through the real chat turn and render what they produced.

Shared by ``test/test_turn_golden.py``, which compares the result with a committed
golden file, and ``scripts/update_turn_golden_snapshots.py``, which rewrites those
files. The split mirrors ``acp_frame_replay_harness``: the test never writes into
the repository, so the writer is a script.

A scenario is one fixture from ``test/fixtures/acp_frames/``. Its frames go through
the production parsers (``classify_notification`` and ``parse_session_update``)
into the events a provider yields, and those events are streamed through
``chat_runner._run_chat``, the entry every dashboard turn takes. Two dashboard-user
socket doubles are registered on the state. ``viewer`` is the tab that shows the
slot and ``bystander`` is a tab that does not. Today both receive the same frames,
written once (see :data:`SAME_AS_VIEWER`), because no frame is scoped to a watched
slot yet. A change that scopes stream frames marks ``viewer`` as watching in
:func:`_sockets` and rewrites the golden files, and the diff then shows exactly
which frames ``bystander`` stopped getting.

What the golden file pins, per socket: every frame in send order, its type and its
payload. Per turn: every line of the transcript file the turn wrote, with the
production key order kept, so a byte-level change to the transcript shows as a
diff. Values that differ between two runs of the same code are replaced (see
:data:`ID_KEYS` and :data:`VALUE_KEYS`). A random id becomes ``<key:n>``, numbered
by first appearance, which keeps the relations the client relies on (a
``chat_chunk``'s ``gen`` equals the ``chat_done``'s ``turn_gen``); a stamp or a
duration becomes ``<key>``.

Determinism. ``slots_push._slots_broadcast_clock`` is replaced by a clock that
moves one hour per read, so every ``push_slots_update`` is a leading-edge
broadcast and no frame depends on a coalescing timer. The provider is a scripted
double, so no process is spawned and no model is called.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from acp_frame_replay_harness import CORPUS, read_fixture
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream

from kiro_crew.acp._dispatch import classify_notification, parse_session_update
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.dashboard import state as dashboard_state

#: The golden files' directory. Resolved from this file so script and test agree.
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "turn_golden"

#: The fixtures replayed as turns, as paths under ``CORPUS``. Each is a live
#: capture of one complete turn that raises no permission request, so the turn
#: runs to its stop reason without a card waiting on an answer. Between them they
#: reach a text-only answer, an MCP tool call with its result and a later status
#: update, and a built-in tool call.
SCENARIOS: tuple[str, ...] = (
    "kiro/saved-spec-live.jsonl",
    "deepseek/turn-live.jsonl",
    "opencode/mcp-directive-call-live.jsonl",
)

#: The slot key every scenario runs on. Fixed so it reads the same in every file.
SLOT_KEY = "chat-golden-1"

#: Random ids. Each distinct value becomes ``<key:n>``, numbered in order of first
#: appearance, so the file still shows which frames carry the same id.
ID_KEYS = frozenset({"gen", "mid", "tab_id", "turn_gen"})

#: Wall-clock stamps, durations and process-wide counters (``governanceGeneration``,
#: ``gitlabHostsGeneration`` and ``tags_revision`` count events of the whole worker,
#: so an earlier test on the same worker moves them). Any set value becomes
#: ``<key>``. They are not numbered: two stamps written back to back can be equal on
#: a coarse clock, so whether two of them match is not stable between runs.
VALUE_KEYS = frozenset(
    {
        "created_at",
        "elapsed_ms",
        "gitlabHostsGeneration",
        "governanceGeneration",
        "last_active",
        "last_activity",
        "tags_revision",
        "ts",
        "ttft_ms",
        "updated_at",
    }
)

#: Keys removed wherever they appear. ``turn_stats`` is the footer's timing record,
#: and whether it is written at all depends on the wall clock: a turn that took 0 ms
#: by the clock writes none, which a coarse clock such as Windows' 15.6 ms tick
#: produces for a replayed turn. Keeping it would make the golden depend on the host.
DROP_KEYS = frozenset({"turn_stats"})

#: An ISO-8601 stamp inside any other string value becomes ``<stamp>``.
_ISO_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d|Z)?")


def golden_path(scenario: str) -> Path:
    """Return the golden file for a scenario, ``<backend>__<name>.golden.json``."""
    backend, name = scenario.split("/", 1)
    return GOLDEN_DIR / f"{backend}__{Path(name).stem}.golden.json"


def turn_events(scenario: str) -> list[AcpEvent]:
    """Return the events a provider yields for one recorded turn, in order.

    Session-update notifications go through ``parse_session_update`` with
    per-replay caches, as the runtime's reader loop does. The response carrying
    ``stopReason`` becomes the terminal ``complete`` event the provider emits for
    it. Other frames (the handshake, metadata notifications) are not events of the
    turn and are dropped.
    """
    _meta, frames = read_fixture(CORPUS / scenario)
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "cache_scope": "replay",
    }
    events: list[AcpEvent] = []
    stop_reason = ""
    for raw in frames:
        msg = JsonRpcMessage.from_dict(raw)
        if msg.method is None:
            result = msg.result if isinstance(msg.result, dict) else {}
            if "stopReason" in result:
                stop_reason = str(result["stopReason"])
            continue
        if classify_notification(msg) != "update":
            continue
        params = msg.params if isinstance(msg.params, dict) else {}
        update = params.get("update")
        if isinstance(update, dict):
            events.extend(parse_session_update(update, **caches))
    if not stop_reason:
        raise ValueError(f"{scenario} has no stopReason response, so it is not a whole turn")
    events.append(_complete(stop_reason))
    return events


class SocketDouble(dict):
    """A dashboard-user WebSocket that records every frame it is sent.

    A ``dict`` because the hub reads and writes per-socket state with ``ws[...]``
    and ``ws.get``. Hashed by identity, as ``aiohttp``'s response is, because the
    hub keeps sockets in sets.
    """

    def __init__(self) -> None:
        super().__init__(_is_dashboard_user=True)
        self.closed = False
        self.frames: list[dict[str, Any]] = []

    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_str(self, msg: str) -> None:
        self.frames.append(json.loads(msg))


def _sockets() -> dict[str, SocketDouble]:
    return {"viewer": SocketDouble(), "bystander": SocketDouble()}


class _Normalizer:
    """Replace run-dependent values with stable placeholders.

    An empty or ``None`` value is kept as it is under every key, so whether a field
    is set stays pinned even where its value is not.
    """

    def __init__(self) -> None:
        self._seen: dict[str, dict[Any, str]] = {}

    def _label(self, key: str, value: Any) -> str:
        table = self._seen.setdefault(key, {})
        marker = json.dumps(value, sort_keys=True)
        if marker not in table:
            table[marker] = f"<{key}:{len(table) + 1}>"
        return table[marker]

    def __call__(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: self._field(k, v) for k, v in value.items() if k not in DROP_KEYS}
        if isinstance(value, list):
            return [self(v, key) for v in value]
        if isinstance(value, str):
            return _ISO_STAMP.sub("<stamp>", value)
        return value

    def _field(self, key: str, value: Any) -> Any:
        if value is None or value == "":
            return value
        if key in ID_KEYS:
            return self._label(key, value)
        if key in VALUE_KEYS:
            return f"<{key}>"
        return self(value, key)


class _SteppingClock:
    """A monotonic clock that moves one hour per read, so no window ever holds."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.reads * 3600.0


async def _drain_sends() -> None:
    """Let every fire-and-forget send task the turn scheduled run."""
    for _ in range(50):
        await asyncio.sleep(0)


async def run_scenario(scenario: str, tmp_path: Path) -> dict[str, Any]:
    """Run one recorded turn and return its golden form."""
    state, client = _runner_state(tmp_path)
    # The coverage helpers stub the fan-out; the golden needs the real one, so
    # drop the instance stubs and let the class methods run.
    for name in (
        "broadcast_ws",
        "push_slots_update",
        "push_refresh",
        "broadcast_context_usage",
        "refresh_slot_source_status",
    ):
        state.__dict__.pop(name, None)
    client.served_model = ""
    client.client = client
    client.pop_pending_oauth_requests = MagicMock(return_value=[])
    sockets = _sockets()
    state._ws_clients.extend(sockets.values())
    _set_stream(client, turn_events(scenario))
    clock = _SteppingClock()
    # Patched before the slot is created: its creation push stamps the window,
    # and a stamp from the real clock would sit hours past the stepping one, so
    # the turn's next push would arm a trailing timer the test never runs.
    # Patched through the ``state`` facade, which mirrors the name to every owner
    # that binds it; patching one owner would leave the others on the real clock.
    with patch.object(dashboard_state, "_slots_broadcast_clock", clock):
        slot = state.get_or_create_slot(SLOT_KEY)
        # Titled, so the end-of-turn cycle runs no titling call.
        slot._titled = True
        await _drive(state, slot, "hello")
        await _drain_sends()
    if state._slots_broadcast_timer is not None:
        state._slots_broadcast_timer.cancel()
        raise AssertionError("a trailing slots flush is still armed, so a frame was deferred")
    if not clock.reads:
        # The push path stopped reading this clock, so coalescing timers decide
        # the slots frames again and the golden would flake. Find the new seam.
        raise AssertionError("slots_push._slots_broadcast_clock was never read during the turn")
    normalize = _Normalizer()
    return {
        "scenario": scenario,
        "sockets": _render_sockets({name: normalize(ws.frames) for name, ws in sockets.items()}),
        "transcript": {
            path.name: [_transcript_line(line, normalize) for line in _lines(path)]
            for path in sorted(tmp_path.glob("*.jsonl"))
        },
    }


#: Written in place of a socket's frame list when it is equal to ``viewer``'s.
SAME_AS_VIEWER = "<same frames as viewer>"


def _render_sockets(frames: dict[str, list[Any]]) -> dict[str, Any]:
    """Keep ``viewer``'s frames and write any socket that received the same list as
    :data:`SAME_AS_VIEWER`, so the file does not hold two copies of one stream. A
    socket whose frames differ keeps its full list, and the diff shows both."""
    viewer = frames["viewer"]
    return {
        name: SAME_AS_VIEWER if name != "viewer" and got == viewer else got
        for name, got in frames.items()
    }


def _lines(path: Path) -> list[str]:
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise AssertionError(f"{path.name} does not end with a newline")
    return raw.decode("utf-8").splitlines()


def _transcript_line(line: str, normalize: _Normalizer) -> str:
    """Return one transcript line with its volatile values replaced.

    The line is parsed to replace values, then dumped again with ``json.dumps``
    defaults. The check below makes that lossless: production writes each line in
    exactly that form (key order, separators, ASCII escapes), so a change to how a
    line is encoded fails here by name rather than vanishing in the re-dump.
    """
    parsed = json.loads(line)
    if json.dumps(parsed) != line:
        raise AssertionError(f"transcript line is no longer in json.dumps form: {line[:200]}")
    return json.dumps(normalize(parsed))


def render(golden: dict[str, Any]) -> str:
    """Render a golden result in its on-disk form.

    Frames keep the key order production wrote them in, so a reordered payload
    shows as a diff, and the file stays readable one frame per block.
    """
    return json.dumps(golden, indent=2, ensure_ascii=False) + "\n"

"""Kiro Crew's pi gate extension delivering steers, run under Node against a stand-in pi.

The extension reads the host's sealed steer files at each ``turn_end`` and queues the
oldest undelivered one with ``pi.sendMessage(message, {deliverAs: "steer"})``, then
notifies a consumed envelope when pi starts that message. These cases load the
SHIPPED file and drive its handlers the way pi does, with files sealed by the host's
own :class:`~kiro_crew.acp.harness.pi_steer.PiSteerChannel`, and pin each guard: the
key, the nonce, the session, the sequence in the name, replay, a turn that aborted
or failed, an idle agent, and a steer retired when its run settled.

Marked ``real_adapter`` like ``test_pi_gate_extension_mcp.py``: the lane that installs
Node 24 runs it, and fails rather than skips when Node is absent there.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from real_adapter_gate import require_real_adapter
from test_pi_gate_extension_mcp import _node

from kiro_crew.acp.harness.pi import pi_gate_extension_path
from kiro_crew.acp.harness.pi_steer import PiSteerChannel

pytestmark = pytest.mark.real_adapter

_NONCE = "0123456789abcdef0123456789abcdef"
_KEY = bytes(range(32))
_SID = "pi-session-1"

# Drives the extension through a list of steps read from argv[3].
_HARNESS = r"""
import { copyFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const [, , extPath, stepsPath, gateDir] = process.argv;
const steps = JSON.parse(readFileSync(stepsPath, "utf8"));
const handlers = {};
const sent = [];
const notes = [];
let streaming = true;
const queued = [];
const pi = {
  registerCommand() {},
  registerMcpServer() {},
  getMcpServers() { return []; },
  on(event, fn) { handlers[event] = fn; },
  sendUserMessage() { throw new Error("sendUserMessage awaits input handlers; not used"); },
  // pi's sendCustomMessage: queued at once while streaming, appended otherwise.
  sendMessage(message, options) {
    if (!streaming) throw new Error("a steer was queued on an idle agent");
    const text = message.content.map((p) => p.text).join("");
    sent.push({ text, deliverAs: options?.deliverAs ?? null, triggerTurn: options?.triggerTurn });
    queued.push({ role: "custom", ...message });
  },
};
function ctx(step) {
  return {
    cwd: gateDir,
    hasUI: true,
    isIdle: () => Boolean(step.idle),
    signal: { aborted: Boolean(step.signalAborted) },
    hasPendingMessages: () => queued.length > 0,
    sessionManager: { getSessionId: () => step.session ?? "pi-session-1" },
    ui: { notify(message, level) { notes.push({ message, level }); }, async confirm() { return true; } },
  };
}
const mod = await import(pathToFileURL(extPath).href);
mod.default(pi);
for (const step of steps) {
  if (step.write) {
    copyFileSync(step.write.src, join(gateDir, step.write.name));
  } else if (step.turn_end) {
    streaming = !step.idle;
    await handlers.turn_end(
      {
        type: "turn_end",
        outcome: step.outcome ?? "completed",
        message: { role: "assistant", stopReason: step.stopReason ?? "stop" },
      },
      ctx(step),
    );
  } else if (step.message_start !== undefined) {
    // pi starts the next queued message, or a plain user message of that text.
    const next = queued[0];
    const isNext = next && next.content.map((p) => p.text).join("") === step.message_start;
    if (isNext) queued.shift();
    const message = isNext
      ? next
      : { role: "user", content: [{ type: "text", text: step.message_start }] };
    await handlers.message_start({ type: "message_start", message }, ctx(step));
  } else if (step.settled) {
    await handlers.agent_settled({ type: "agent_settled", aborted: false }, ctx(step));
  }
}
console.log(JSON.stringify({ sent, notes }));
"""


@pytest.fixture
def node() -> list[str]:
    argv = _node()
    require_real_adapter(argv, what="Node 22.6 or later", install="install Node 24")
    assert argv is not None
    return argv


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.gate = tmp_path / "pi-gate"
        self.gate.mkdir(mode=0o700)
        self.staged = tmp_path / "staged"
        self.staged.mkdir()
        self.channel = PiSteerChannel(nonce=_NONCE, artifact_dir=str(self.gate), key=_KEY)
        self.stem = os.path.basename(self.channel._stem())
        self.steps: list[dict[str, Any]] = []

    def write(
        self,
        text: str,
        *,
        seq: int | None = None,
        name_seq: int | None = None,
        session: str = _SID,
        key: bytes = _KEY,
        nonce: str = _NONCE,
    ) -> int:
        """Seal *text* as the host does and stage a step that lands it in the gate dir."""
        sealer = PiSteerChannel(nonce=nonce, artifact_dir=str(self.gate), key=key)
        sealer.seq = (seq if seq is not None else self.channel.seq + 1) - 1
        got, payload = sealer.seal(session, text)
        self.channel.seq = max(self.channel.seq, got)
        name = f"{self.stem}{(name_seq or got):010d}.json"
        src = self.staged / f"{len(self.steps)}-{name}"
        src.write_bytes(payload)
        self.steps.append({"write": {"src": str(src), "name": name}})
        return got

    def turn_end(self, **kw: Any) -> None:
        self.steps.append({"turn_end": True, **kw})

    def message_start(self, text: str) -> None:
        self.steps.append({"message_start": text})

    def settled(self) -> None:
        self.steps.append({"settled": True})

    def run(self, node_argv: list[str]) -> dict[str, Any]:
        harness = self.tmp / "harness.mjs"
        harness.write_text(_HARNESS, encoding="utf-8")
        extension = self.tmp / "kiro_crew_tool_gate.ts"
        extension.write_bytes(Path(pi_gate_extension_path()).read_bytes())
        steps = self.tmp / "steps.json"
        steps.write_text(json.dumps(self.steps), encoding="utf-8")
        home = self.tmp / "home"
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("PI_", "KIROCREW_PI_"))},
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PI_CODING_AGENT_DIR": str(self.tmp / "agent"),
            "KIROCREW_PI_GATE_SESSION": _NONCE,
            **self.channel.env(),
        }
        completed = subprocess.run(
            [*node_argv, str(harness), str(extension), str(steps), str(self.gate)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.strip().splitlines()[-1])


def _sent(out: dict[str, Any]) -> list[str]:
    # Queued as a steer, and never with triggerTurn: false, which pi would hold to
    # the end of the turn instead of reading before the next model call.
    assert all(s["deliverAs"] == "steer" and s.get("triggerTurn") is None for s in out["sent"])
    return [s["text"] for s in out["sent"]]


def _echoes(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(n["message"]) for n in out["notes"] if "kiro-crew-steer" in n["message"]]


def test_a_steer_is_delivered_as_a_steer_and_its_start_is_echoed(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("use blue")
    rig.turn_end()
    rig.message_start("an unrelated user message")
    rig.message_start("use blue")
    out = rig.run(node)
    assert _sent(out) == ["use blue"]
    assert _echoes(out) == [{"kiro-crew-steer": 1, "nonce": _NONCE, "seq": 1}]
    assert out["notes"][0]["level"] == "info"


def test_steers_go_oldest_first_one_per_turn_end(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("second", seq=2)
    rig.write("first", seq=1)
    rig.turn_end()
    rig.message_start("first")
    rig.turn_end()
    rig.message_start("second")
    out = rig.run(node)
    assert _sent(out) == ["first", "second"]
    assert [e["seq"] for e in _echoes(out)] == [1, 2]


@pytest.mark.parametrize(
    "forged",
    [
        {"key": bytes(32)},
        {"nonce": "f" * 32},
        {"session": "another-session"},
        {"name_seq": 2},
    ],
    ids=["another-key", "another-nonce", "another-session", "renamed"],
)
def test_a_steer_that_does_not_open_for_this_launch_is_never_delivered(
    node, tmp_path, forged
) -> None:
    rig = _Rig(tmp_path)
    rig.write("forged", **forged)
    rig.turn_end()
    rig.turn_end()
    out = rig.run(node)
    assert _sent(out) == []
    assert _echoes(out) == []


def test_a_delivered_steer_is_never_delivered_again(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("use blue")
    rig.turn_end()
    rig.message_start("use blue")
    rig.turn_end()
    # The same sealed bytes landed again (a replay of sequence 1).
    rig.write("use blue", seq=1)
    rig.turn_end()
    rig.message_start("use blue")
    out = rig.run(node)
    assert _sent(out) == ["use blue"]
    assert [e["seq"] for e in _echoes(out)] == [1]


@pytest.mark.parametrize(
    "boundary",
    [
        {"outcome": "aborted"},
        {"outcome": "error"},
        {"stopReason": "aborted"},
        {"stopReason": "error"},
        {"signalAborted": True},
        {"idle": True},
    ],
    ids=["aborted", "error", "aborted-message", "error-message", "abort-signal", "idle"],
)
def test_no_steer_is_handed_to_a_turn_that_will_not_read_it(node, tmp_path, boundary) -> None:
    rig = _Rig(tmp_path)
    rig.write("use blue")
    rig.turn_end(**boundary)
    out = rig.run(node)
    assert _sent(out) == []


def test_a_steer_its_run_settled_without_is_retired_not_carried_over(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("too late")
    rig.settled()
    rig.turn_end()
    rig.write("next turn's")
    rig.turn_end()
    out = rig.run(node)
    assert _sent(out) == ["next turn's"]


def test_a_user_message_with_the_steers_text_is_not_its_echo(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("use blue")
    rig.steps.append({"message_start": "use blue"})  # nothing queued yet: a plain user message
    rig.turn_end()
    rig.message_start("use blue")
    out = rig.run(node)
    assert [e["seq"] for e in _echoes(out)] == [1]


def test_an_echo_is_not_sent_for_a_message_the_extension_did_not_deliver(node, tmp_path) -> None:
    rig = _Rig(tmp_path)
    rig.write("use blue")
    rig.turn_end()
    rig.settled()
    rig.message_start("use blue")
    out = rig.run(node)
    assert _sent(out) == ["use blue"]
    assert _echoes(out) == []
